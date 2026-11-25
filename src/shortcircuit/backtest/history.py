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


def nse_equities(text: Optional[str] = None) -> list[str]:
    """Every NSE EQ-series symbol in Fyers' cash symbol master."""
    if text is None:
        import requests                    # public static CDN, not the trading API
        response = requests.get(NSE_CM_URL, timeout=(5, 30))
        response.raise_for_status()
        text = response.text
    out = set()
    for line in text.splitlines():
        cols = line.split(",")
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
    def __init__(self, client, cache_dir: Path = CACHE_DIR) -> None:
        self.client = client
        self.cache_dir = Path(cache_dir)

    def _path(self, kind: str, symbol: str, key: str) -> Path:
        safe = symbol.replace(":", "_")
        return self.cache_dir / kind / safe / f"{key}.csv"

    def _fetch(self, symbol: str, resolution: str, start: dt.date, end: dt.date) -> pd.DataFrame:
        reply = self.client.history(data={
            "symbol": symbol, "resolution": resolution, "date_format": "1",
            "range_from": start.isoformat(), "range_to": end.isoformat(), "cont_flag": "1",
        })
        if not isinstance(reply, dict) or reply.get("s") not in ("ok", "no_data"):
            raise RuntimeError(f"history {symbol} {resolution} {start}..{end}: {reply}")
        return _frame(reply.get("candles"))

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


def gainer_days(history: History, symbols: Iterable[str], start: dt.date, end: dt.date,
                min_gain_pct: float, min_price: float, min_volume: float,
                progress_every: int = 100) -> list[tuple[str, dt.date, float]]:
    """
    (symbol, date, prev_close) for every session in which the stock traded at
    least `min_gain_pct` above the previous close — the days a top-gainer scan
    would have shown it.
    """
    # One day earlier than `start` so the first session has a previous close.
    lead = start - dt.timedelta(days=10)
    out = []
    symbols = list(symbols)
    for n, symbol in enumerate(symbols, 1):
        try:
            df = history.daily(symbol, lead, end)
        except Exception as exc:
            logger.warning("[BACKTEST] daily %s failed: %s", symbol, exc)
            continue
        if not df.empty:
            hit = df[(df["date"] >= start) & (df["prev_close"] >= min_price)
                     & (df["high"] >= df["prev_close"] * (1 + min_gain_pct / 100))
                     & (df["volume"] >= min_volume)]
            out.extend((symbol, d, float(pc)) for d, pc in zip(hit["date"], hit["prev_close"], strict=True))
        if progress_every and n % progress_every == 0:
            logger.info("[BACKTEST] universe: %d/%d symbols scanned, %d gainer-days so far",
                        n, len(symbols), len(out))
    return sorted(out, key=lambda x: (x[1], x[0]))
