"""Command line: python -m shortcircuit.backtest --help"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys

import pandas as pd

from shortcircuit import config
from shortcircuit.backtest.history import History, gainer_days, nse_equities
from shortcircuit.backtest.replay import Costs, replay_day, summarise
from shortcircuit.eod import squareoff
from shortcircuit.paths import REPORTS_DIR
from shortcircuit.runtime import mode as run_mode
from shortcircuit.strategy import registry

logger = logging.getLogger("shortcircuit.backtest")


def _date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def parse(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m shortcircuit.backtest",
                                description="Replay the active strategies on Fyers history. "
                                            "Data-only: never places an order.")
    p.add_argument("--from", dest="start", type=_date, required=True, help="first session, YYYY-MM-DD")
    p.add_argument("--to", dest="end", type=_date, required=True, help="last session, YYYY-MM-DD")
    p.add_argument("--symbols", help="comma-separated NSE:XYZ-EQ list; default: every NSE EQ stock")
    p.add_argument("--strategies", help="comma-separated names; default: the active set")
    p.add_argument("--min-gain", type=float, default=10.0,
                   help="gainer-day threshold, %% over previous close, on the day's high (default 10)")
    p.add_argument("--slippage", type=float, default=Costs.slippage_pct,
                   help="%% per fill against the trade (default %(default)s)")
    p.add_argument("--cost", type=float, default=Costs.cost_pct,
                   help="%% per round trip, charges and brokerage (default %(default)s)")
    p.add_argument("--max-trades-per-day", type=int, default=1)
    p.add_argument("--out", help="trades CSV path; default reports/backtest/<strategies>_<from>_<to>.csv")
    return p.parse_args(argv)


def force_data_only() -> None:
    """A backtest can never place an order, whatever the environment says.
    Read per request by the transport, so this holds from the next request on."""
    os.environ[run_mode.ENV] = run_mode.Mode.DATA_ONLY.value


def connect():
    force_data_only()
    from shortcircuit.broker.fyers_connect import FyersConnect
    conn = FyersConnect(config)
    if not conn.fyers:
        raise SystemExit("Fyers login failed — see the log above.")
    return conn.fyers


def run(args, client, symbols_text=None) -> tuple[pd.DataFrame, dict]:
    force_data_only()
    names =[n.strip() for n in args.strategies.split(",")] if args.strategies else None
    strategies = registry.load(names)
    if not strategies:
        raise SystemExit("No strategy to backtest: none active and none named with --strategies.")
    costs = Costs(slippage_pct=args.slippage, cost_pct=args.cost)
    history = History(client)
    squareoff.registry.refresh()

    symbols = ([s.strip() for s in args.symbols.split(",") if s.strip()]
               if args.symbols else nse_equities(symbols_text))
    logger.info("[BACKTEST] %s on %d symbols, %s → %s", ", ".join(s.name for s in strategies),
                len(symbols), args.start, args.end)
    days = gainer_days(history, symbols, args.start, args.end, args.min_gain,
                       config.SCANNER_MIN_LTP, config.SCANNER_MIN_VOLUME)
    logger.info("[BACKTEST] %d gainer-days to replay", len(days))

    trades, signals, invalidated, expired = [], 0, 0, 0
    for n, (symbol, day, prev_close) in enumerate(days, 1):
        try:
            bars = history.minute(symbol, day)
        except Exception as exc:
            logger.warning("[BACKTEST] minute %s %s failed: %s", symbol, day, exc)
            continue
        result = replay_day(symbol, bars, prev_close, strategies, costs, args.max_trades_per_day)
        signals += result.signals
        invalidated += result.invalidated
        expired += result.expired
        trades.extend(result.trades)
        if n % 25 == 0:
            logger.info("[BACKTEST] replayed %d/%d days — %d trades", n, len(days), len(trades))

    frame = pd.DataFrame([t.row(costs) for t in trades])
    summary = {"gainer_days": len(days), "signals": signals, "gate_invalidated": invalidated,
               "gate_expired": expired, **summarise(trades, costs),
               "slippage_pct_per_fill": costs.slippage_pct, "cost_pct_round_trip": costs.cost_pct}
    return frame, summary


def main(argv=None) -> int:
    force_data_only()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    args = parse(argv)
    if args.end < args.start:
        raise SystemExit("--to is before --from")
    frame, summary = run(args, connect())

    names = args.strategies.replace(",", "+") if args.strategies else "active"
    out = args.out or str(REPORTS_DIR / "backtest" / f"{names}_{args.start}_{args.end}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    frame.to_csv(out, index=False)
    with open(out.replace(".csv", "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nTrades: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
