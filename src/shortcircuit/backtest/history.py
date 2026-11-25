"""
Historical candles for the backtest: fetched once from the Fyers history API,
then served from a cache under data/backtest/.

History is a data API, so it works in data-only mode and without a static IP.
Every request goes through the metered transport, so a long fetch is paced by
the same rate limiter as the bot and cannot get the account throttled.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

from shortcircuit.paths import DATA_DIR

logger = logging.getLogger(__name__)

CACHE_DIR = DATA_DIR / "backtest"
IST = "Asia/Kolkata"
COLUMNS = ["epoch", "open", "high", "low", "close", "volume"]
NSE_CM_URL = "https://public.fyers.in/sym_details/NSE_CM.csv"
DAILY_SPAN_DAYS = 365                     # Fyers: up to 366 days per daily request
# Fyers answers a throttled request with code -360 or 429, and during a backend
# blip answers every request with -99 "Bad request" (25 Nov 2026, 19:00-19:02:
# 1,360 in a row, then normal). Both are retried after a pause, never dropped;
# -99 never appeared outside that window.
RETRY_CODES = (-360, 429, -99)
RETRY_WAITS_S = (5, 15, 30, 60, 120)


# Column 3 of the cash master is the exchange instrument type: 0 is a stock, 9
# an ETF. ETFs share the -EQ suffix, and their "gainer days" are a premium to
# the underlying's value, not a move any intraday stock setup is about.
STOCK_TYPE = "0"


def nse_equities(text: Optional[str] = None) -> list[str]:
    """Every NSE EQ-series stock in Fyers' cash symbol master: ETFs excluded."""
    if text is None:
        import requests                    # public static CDN, not the trading API
        response = requests.get(NSE_CM_URL, timeout=(5, 30))
        response.raise_for_status()
        text = response.text
    out = set()
    for line in text.splitlines():
        cols = line.split(",")
        if len(cols) < 3 or cols[2].strip() != STOCK_TYPE:
            continue
        for idx in (9, 13):
            if len(cols) > idx and cols[idx].startswith("NSE:") and cols[idx].endswith("-EQ"):
                out.add(cols[idx].strip())
                break
    return sorted(out)


def _frame(candles) -> pd.DataFrame:
    df = pd.DataFrame(candles or [], columns=COLUMNS)
    if df.empty:
        return df.assign(datetime=pd.Series(dtype=f"datetime64[ns, {IST}]"))
    df["epoch"] = df["epoch"].astype("int64")
    df["datetime"] = pd.to_datetime(df["epoch"], unit="s", utc=True).dt.tz_convert(IST)
    return df.drop_duplicates("epoch").sort_values("epoch").reset_index(drop=True)


class History:
    def __init__(self, client, cache_dir: Path = CACHE_DIR, sleep=time.sleep) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)
        self._sleep = sleep
        self.failures: list[str] = []          # requests that failed after every retry

    def _path(self, kind: str, symbol: str, key: str) -> Path:
        safe = symbol.replace(":", "_")
        return self.cache_dir / kind / safe / f"{key}.csv"

    def _fetch(self, symbol: str, resolution: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        request = {
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1",
        }
        for wait in (*RETRY_WAITS_S, None):
            reply = self.client.history(data=request)
            if isinstance(reply, dict) and reply.get("s") in ("ok", "no_data"):
                return _frame(reply.get("candles"))
            throttled = isinstance(reply, dict) and reply.get("code") in RETRY_CODES
            if not throttled or wait is None:
                break
            logger.info("[BACKTEST] throttled on %s %s; retrying in %ds", symbol, resolution, wait)
            self._sleep(wait)
        self.failures.append(f"{symbol} {resolution} {start}..{end}")
        raise RuntimeError(f"history {symbol} {resolution} {start}..{end}: {reply}")

    def _cached(self, path: Path, fetch) -> pd.DataFrame:
        if path.is_file():
            return _frame(pd.read_csv(path)[COLUMNS].values.tolist())
        df = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        df[COLUMNS].to_csv(path, index=False)
        return df

    def daily(self, symbol: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        """Daily bars, with `date` and `prev_close` columns. Cached per range."""
        def fetch():
            parts, s = [], start
            while s <= end:
                e = min(end, s + dt.timedelta(days=DAILY_SPAN_DAYS - 1))
                parts.append(self._fetch(symbol, "D", s, e))
                s = e + dt.timedelta(days=1)
            return pd.concat(parts, ignore_index=True) if parts else _frame([])
        df = self._cached(self._path("daily", symbol, f"{start}_{end}"), fetch)
        if df.empty:
            return df.assign(date=pd.Series(dtype=object), prev_close=pd.Series(dtype=float))
        df["date"] = df["datetime"].dt.date
        df["prev_close"] = df["close"].shift(1)
        return df

    def minute(self, symbol: str, day: dt.date) -> pd.DataFrame:
        """One session's 1-minute bars, 09:15-15:29, that date only."""
        df = self._cached(self._path("minute", symbol, day.isoformat()),
                          lambda: self._fetch(symbol, "1", day, day))
        if df.empty:
            return df
        # A single-day request has been seen to return two sessions; keep the day's.
        df = df[df["datetime"].dt.date == day]
        t = df["datetime"].dt.time
        return df[(t >= dt.time(9, 15)) & (t < dt.time(15, 30))].reset_index(drop=True)


MINUTE_SPAN_DAYS = 100                    # Fyers: up to 100 days per intraday request


def _windows(days: list[dt.date], span: int = MINUTE_SPAN_DAYS) -> list[list[dt.date]]:
    """Group sorted dates into runs that each fit one request of `span` days."""
    out: list[list[dt.date]] = []
    for d in sorted(set(days)):
        if out and (d - out[-1][0]).days < span:
            out[-1].append(d)
        else:
            out.append([d])
    return out


def prefetch_minutes(history: "History", wanted: Iterable[tuple[str, dt.date]],
                     workers: int = 1, progress_every: int = 250) -> int:
    """
    Fill the minute cache for every (symbol, day) in `wanted`, one request per
    100-day window per symbol rather than one per day. A stock that runs often
    runs in clusters, so most windows hold several of its days. Returns the
    number of requests made; days already cached cost nothing.
    """
    by_symbol: dict[str, list[dt.date]] = {}
    for symbol, day in wanted:
        if not history._path("minute", symbol, day.isoformat()).is_file():
            by_symbol.setdefault(symbol, []).append(day)
    tasks = [(s, w) for s, ds in by_symbol.items() for w in _windows(ds)]

    def fetch(task):
        symbol, days = task
        try:
            df = history._fetch(symbol, "1", days[0], days[-1])
        except Exception as exc:
            logger.warning("[BACKTEST] minute %s %s..%s failed: %s", symbol, days[0], days[-1], exc)
            return
        dates = df["datetime"].dt.date if not df.empty else pd.Series(dtype=object)
        for d in days:
            path = history._path("minute", symbol, d.isoformat())
            path.parent.mkdir(parents=True, exist_ok=True)
            df.loc[dates == d, COLUMNS].to_csv(path, index=False)

    logger.info("[BACKTEST] minute bars: %d days to fetch in %d requests",
                sum(len(w) for _, w in tasks), len(tasks))
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for n, _ in enumerate(pool.map(fetch, tasks), 1):
            if progress_every and n % progress_every == 0:
                logger.info("[BACKTEST] minute bars: %d/%d requests", n, len(tasks))
    return len(tasks)


def gainer_days(history: History, symbols: Iterable[str], start: dt.date, end: dt.date,
                min_gain_pct: float, min_price: float, min_volume: float,
                progress_every: int = 100, workers: int = 1) -> list[tuple[str, dt.date, float]]:
    """
    (symbol, date, prev_close) for every session in which the stock traded at
    least `min_gain_pct` above the previous close — the days a top-gainer scan
    would have shown it.
    """
    # One day earlier than `start` so the first session has a previous close.
    lead = start - dt.timedelta(days=10)
    out = []
    symbols = list(symbols)

    def load(symbol):
        try:
            return history.daily(symbol, lead, end)
        except Exception as exc:
            logger.warning("[BACKTEST] daily %s failed: %s", symbol, exc)
            return None

    # Requests are latency-bound (~1s each); a few in flight keep the account's
    # rate limiter, not the round trip, as the bound. The limiter still meters
    # every one of them.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        frames = pool.map(load, symbols)
        for n, (symbol, df) in enumerate(zip(symbols, frames, strict=True), 1):
            if df is not None and not df.empty:
                hit = df[(df["date"] >= start) & (df["prev_close"] >= min_price)
                         & (df["high"] >= df["prev_close"] * (1 + min_gain_pct / 100))
                         & (df["volume"] >= min_volume)]
                out.extend((symbol, d, float(pc)) for d, pc in zip(hit["date"], hit["prev_close"], strict=True))
            if progress_every and n % progress_every == 0:
                logger.info("[BACKTEST] universe: %d/%d symbols scanned, %d gainer-days so far",
                            n, len(symbols), len(out))
    return sorted(out, key=lambda x: (x[1], x[0]))
