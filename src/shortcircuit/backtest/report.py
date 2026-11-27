"""
The backtest dashboard: one self-contained HTML page per run.

    python -m shortcircuit.backtest.report reports/backtest/<run>.csv

The CLI writes it after every run. It reads the run's three files (trades CSV,
`_summary.json`, `_signals.csv`) and the minute bars already cached under
data/backtest/, so it needs no login and makes no request to the broker.

Python only prepares data. Every statistic on the page is computed in the page
from the per-trade returns, so the leverage and slot toggles recompute all of
it consistently. Charts: TradingView Lightweight Charts for prices and the
account curve, Apache ECharts for the rest, both Apache-2.0, loaded from
jsDelivr pinned by version and subresource-integrity hash.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
from pathlib import Path
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

DEFAULT_LEVERAGE = 4.0
# Each charted trade embeds a session of minute bars (~19 KB). A multi-year run
# has thousands of trades, so only the most recent, the best and the worst are
# charted; every trade still counts in every statistic and table.
CHART_RECENT, CHART_EXTREMES = 600, 100
IST_OFFSET_S = 19_800             # charts draw UTC; shifting by +5:30 makes their axis read IST
TEMPLATE = Path(__file__).with_name("dashboard.html")

LIBS = {
    "lwc": ("https://cdn.jsdelivr.net/npm/lightweight-charts@4.2.3/dist/"
            "lightweight-charts.standalone.production.js",
            "sha384-stKllnUqA9AD0gsKCuUtf5XlqAW7PwIgDagoNsTWkjkBmJ/GZ/uHTgEBxdLV2VSK"),
    "echarts": ("https://cdn.jsdelivr.net/npm/echarts@5.5.1/dist/echarts.min.js",
                "sha384-Mx5lkUEQPM1pOJCwFtUICyX45KNojXbkWdYhkKUKsbv391mavbfoAmONbzkgYPzR"),
}

# Trade CSV columns the page reads by name; anything else is a strategy feature.
_ENGINE_COLS = {
    "date", "symbol", "strategy", "pattern", "signal_time", "entry_time", "entry", "stop",
    "exit_reason", "exit_time", "legs", "gross_pct", "net_pct", "mfe_pct", "mae_pct",
    "exit_model", "trigger", "stop_reference", "legs_json", "stop_path_json", "direction",
}


def _chart_time(ts) -> int:
    """An IST wall-clock time as the chart's UTC-epoch axis value."""
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("Asia/Kolkata")
    return int(t.timestamp()) + IST_OFFSET_S


def _at(date: str, hhmm: str) -> int:
    return _chart_time(f"{date} {hhmm}")


def _clean(v):
    if isinstance(v, float) and not math.isfinite(v):
        return None
    if hasattr(v, "item"):
        return v.item()
    return v


def _rsi(closes: pd.Series, period: int = 14) -> list:
    """Wilder RSI at every bar, the series form of features.compute_rsi_wilder."""
    d = closes.diff()
    up = d.clip(lower=0).ewm(alpha=1 / period, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / period, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, float("nan")))
    rsi = rsi.where(dn != 0, 100.0)
    out = [None if (i < period or not math.isfinite(x)) else round(float(x), 2)
           for i, x in enumerate(rsi)]
    return out


def _bars(history, symbol: str, date: str) -> Optional[dict]:
    try:
        df = history.minute(symbol, dt.date.fromisoformat(date))
    except Exception as exc:                 # not cached: the page shows the trade without a chart
        logger.debug("[REPORT] no bars for %s %s: %s", symbol, date, exc)
        return None
    if df is None or df.empty:
        return None
    return {
        "t": [int(e) + IST_OFFSET_S for e in df["epoch"]],
        "o": df["open"].round(2).tolist(), "h": df["high"].round(2).tolist(),
        "l": df["low"].round(2).tolist(), "c": df["close"].round(2).tolist(),
        "v": df["volume"].astype("int64").tolist(),
        "rsi": _rsi(df["close"].astype(float)),
    }


def _feature_marks(date: str, feats: dict) -> list:
    """Strategy features that are times on the trade's day become chart markers."""
    marks = []
    for k, v in feats.items():
        if not isinstance(v, str) or len(v) < 16:
            continue
        try:
            t = pd.Timestamp(v)
        except (ValueError, TypeError):
            continue
        if t.date().isoformat() == date:
            marks.append({"t": _chart_time(t), "label": k})
    return marks


def _charted(trades: pd.DataFrame, recent: int, extremes: int) -> set:
    """Row positions whose minute bars are embedded."""
    if len(trades) <= recent + 2 * extremes:
        return set(range(len(trades)))
    order = pd.to_datetime(trades["date"] + " " + trades["entry_time"]).reset_index(drop=True)
    net = trades["net_pct"].reset_index(drop=True)
    keep = set(order.nlargest(recent).index) | set(net.nlargest(extremes).index)         | set(net.nsmallest(extremes).index)
    return keep


def payload(trades: pd.DataFrame, summary: dict, signals: pd.DataFrame, history,
            recent: int = CHART_RECENT, extremes: int = CHART_EXTREMES) -> dict:
    rows, bars = [], {}
    feature_cols = [c for c in trades.columns if c not in _ENGINE_COLS]
    charted = _charted(trades, recent, extremes)
    for n, r in enumerate(trades.to_dict("records")):
        date, sym = str(r["date"]), str(r["symbol"])
        legs = json.loads(r.get("legs_json") or "[]") if isinstance(r.get("legs_json"), str) else []
        path = json.loads(r.get("stop_path_json") or "[]") if isinstance(r.get("stop_path_json"), str) else []
        feats = {c: _clean(r[c]) for c in feature_cols if _clean(r.get(c)) is not None}
        entry_t = _at(date, r["entry_time"])
        exit_t = _chart_time(legs[-1][3]) if legs else (_at(date, r["exit_time"])
                                                        if isinstance(r.get("exit_time"), str) else entry_t)
        key = f"{sym}|{date}"
        if key not in bars and history is not None and n in charted:
            bars[key] = _bars(history, sym, date)
        rows.append({
            "id": n, "date": date, "symbol": sym, "strategy": r.get("strategy"),
            # Runs from before per-strategy directions have no column: all shorts.
            "direction": r.get("direction") if isinstance(r.get("direction"), str) else "SHORT",
            "pattern": r.get("pattern"), "key": key,
            "signal_t": _at(date, r["signal_time"]), "entry_t": entry_t, "exit_t": exit_t,
            "entry": _clean(r["entry"]), "stop": _clean(r["stop"]),
            "trigger": _clean(r.get("trigger")), "stop_ref": _clean(r.get("stop_reference")),
            "reason": r["exit_reason"], "net": _clean(r["net_pct"]), "gross": _clean(r["gross_pct"]),
            "mfe": _clean(r["mfe_pct"]), "mae": _clean(r["mae_pct"]), "model": r.get("exit_model"),
            "legs": [{"frac": f, "px": p, "reason": why, "t": _chart_time(w)} for f, p, why, w in legs],
            "stops": [{"t": _chart_time(w), "px": p} for w, p in path],
            "features": feats, "marks": _feature_marks(date, feats),
        })
    sig = [] if signals is None or signals.empty else [
        {k: _clean(v) for k, v in s.items()} for s in signals.to_dict("records")]
    return {"summary": summary, "trades": rows, "bars": {k: v for k, v in bars.items() if v},
            "signals": sig, "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}


def render(data: dict, leverage: float = DEFAULT_LEVERAGE) -> str:
    blob = json.dumps(data, separators=(",", ":"), default=str, allow_nan=False)
    blob = blob.replace("</", "<\\/")       # the JSON sits inside a <script> element
    html = TEMPLATE.read_text(encoding="utf-8")
    for name, (url, sri) in LIBS.items():
        html = html.replace(f"{{{{{name}_url}}}}", url).replace(f"{{{{{name}_sri}}}}", sri)
    return html.replace("{{leverage}}", json.dumps(float(leverage))).replace("{{data}}", blob)


def build(trades_csv: str, leverage: float = DEFAULT_LEVERAGE, out: Optional[str] = None,
          cache_dir: Optional[Path] = None) -> str:
    from shortcircuit.backtest import history as hist

    base = str(trades_csv)[:-4] if str(trades_csv).endswith(".csv") else str(trades_csv)
    try:
        trades = pd.read_csv(trades_csv)
    except (pd.errors.EmptyDataError, FileNotFoundError):
        trades = pd.DataFrame()
    summary_path, signals_path = Path(base + "_summary.json"), Path(base + "_signals.csv")
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
    try:
        signals = pd.read_csv(signals_path) if signals_path.is_file() else pd.DataFrame()
    except pd.errors.EmptyDataError:
        signals = pd.DataFrame()
    history = hist.History(None, cache_dir or hist.CACHE_DIR)
    data = payload(trades, summary, signals, history) if not trades.empty else \
        {"summary": summary, "trades": [], "bars": {}, "signals": [],
         "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M")}
    out = out or base + ".html"
    Path(out).write_text(render(data, leverage), encoding="utf-8")
    return out


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m shortcircuit.backtest.report",
                                description="Rebuild a backtest's HTML dashboard from its files.")
    p.add_argument("trades_csv")
    p.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    p.add_argument("--out")
    p.add_argument("--cache-dir", type=Path, help="minute-bar cache; default data/backtest")
    args = p.parse_args(argv)
    print(build(args.trades_csv, args.leverage, args.out, args.cache_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
