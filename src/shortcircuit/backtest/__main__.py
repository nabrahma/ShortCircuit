"""Command line: python -m shortcircuit.backtest --help"""
from __future__ import annotations

import argparse
import datetime as dt
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import sys

import pandas as pd

from shortcircuit import config
from shortcircuit.backtest.history import History, gainer_days, nse_equities, prefetch_minutes
from shortcircuit.backtest import report
from shortcircuit.backtest.replay import (Costs, infer_lower_circuit, infer_upper_circuit,
                                          replay_day, summarise)
from shortcircuit.execution import directions
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
    p.add_argument("--from", dest="start", type=_date, help="first session, YYYY-MM-DD")
    p.add_argument("--to", dest="end", type=_date, help="last session, YYYY-MM-DD")
    p.add_argument("--login", action="store_true",
                   help="log in to Fyers (browser + auth code), save the day's token, and exit")
    p.add_argument("--symbols", help="comma-separated NSE:XYZ-EQ list; default: every NSE EQ stock")
    p.add_argument("--strategies", help="comma-separated names; default: the active set")
    p.add_argument("--min-gain", type=float, default=10.0,
                   help="mover-day threshold, %% from the previous close: the day's high for "
                        "gainers (short strategies), its low for losers (long ones) (default 10)")
    p.add_argument("--slippage", type=float, default=Costs.slippage_pct,
                   help="%% per fill against the trade (default %(default)s)")
    p.add_argument("--cost", type=float, default=Costs.cost_pct,
                   help="%% per round trip, charges and brokerage (default %(default)s)")
    p.add_argument("--max-trades-per-day", type=int, default=1)
    p.add_argument("--frame-bars", type=int, default=None,
                   help="hand strategies only the last N 1-minute bars, as VWAP_ANCHOR_MODE="
                        "'ROLLING' does live (e.g. 100); default: the whole session")
    p.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) - 1)),
                   help="processes replaying days in parallel (default %(default)s)")
    p.add_argument("--workers", type=int, default=3,
                   help="history requests in flight; the rate limiter bounds the total (default 3)")
    p.add_argument("--out", help="trades CSV path; default reports/backtest/<strategies>_<from>_<to>.csv")
    p.add_argument("--leverage", type=float, default=report.DEFAULT_LEVERAGE,
                   help="MIS leverage the dashboard's account curve assumes (default %(default)s)")
    p.add_argument("--no-report", action="store_true", help="skip the HTML dashboard")
    args = p.parse_args(argv)
    if not args.login and (args.start is None or args.end is None):
        p.error("--from and --to are required unless --login is given")
    return args


def force_data_only() -> None:
    """A backtest can never place an order, whatever the environment says.
    Read per request by the transport, so this holds from the next request on."""
    os.environ[run_mode.ENV] = run_mode.Mode.DATA_ONLY.value


def quiet_engine_logs() -> None:
    """The live analyzer and strategies log per evaluation, which a backtest makes
    once a minute per gainer-day: millions of lines a year. Warnings still show."""
    logging.getLogger("shortcircuit").setLevel(logging.WARNING)
    logging.getLogger("shortcircuit.backtest").setLevel(logging.INFO)
    logging.getLogger("strategies").setLevel(logging.WARNING)


def connect():
    force_data_only()
    from shortcircuit.broker.fyers_connect import FyersConnect
    conn = FyersConnect(config)
    if not conn.fyers:
        raise SystemExit("Fyers login failed — see the log above.")
    return conn.fyers


def run(args, client, symbols_text=None) -> tuple[pd.DataFrame, dict, pd.DataFrame]:
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
    # Gainer days for short strategies, loser days for long ones, by the
    # strategies' universes — the bands the live scanner would watch.
    scan_up, scan_down = directions.scan_bands(strategies)
    days = gainer_days(history, symbols, args.start, args.end, args.min_gain,
                       config.SCANNER_MIN_LTP, config.SCANNER_MIN_VOLUME, workers=args.workers,
                       gainers=scan_up, losers=scan_down)
    bands = "+".join(b for b, on in (("gainers", scan_up), ("losers", scan_down)) if on)
    logger.info("[BACKTEST] %d mover-days (%s) to replay", len(days), bands)
    prefetch_minutes(history, [(s, d) for s, d, _ in days], workers=args.workers)

    def minute(item):
        symbol, day, _ = item
        try:
            return history.minute(symbol, day)
        except Exception as exc:
            logger.warning("[BACKTEST] minute %s %s failed: %s", symbol, day, exc)
            return None

    def jobs():
        """(symbol, bars, prev_close, costs, max_trades, upper_circuit, frame_bars,
        lower_circuit), in day order."""
        fetch = ThreadPoolExecutor(max_workers=max(1, args.workers))
        try:
            for n, ((symbol, _day, prev_close), bars) in enumerate(
                    zip(days, fetch.map(minute, days), strict=True), 1):
                if n % 250 == 0:
                    logger.info("[BACKTEST] minute bars: %d/%d days loaded", n, len(days))
                if bars is None or bars.empty:
                    continue
                fno = squareoff.registry.is_fno(symbol)
                uc = infer_upper_circuit(prev_close, float(bars["high"].max()), fno)
                lc = infer_lower_circuit(prev_close, float(bars["low"].min()), fno)
                yield (symbol, bars, prev_close, costs, args.max_trades_per_day, uc,
                       args.frame_bars, lc)
        finally:
            fetch.shutdown(cancel_futures=True)

    if args.jobs > 1:
        from concurrent.futures import ProcessPoolExecutor
        from shortcircuit.backtest import parallel
        procs = ProcessPoolExecutor(max_workers=args.jobs, initializer=parallel.init_worker,
                                    initargs=([s.name for s in strategies], squareoff.registry.snapshot()))
        results = (f.result() for f in [procs.submit(parallel.replay, j) for j in jobs()])
    else:
        procs = None
        results = (replay_day(sym, bars, pc, strategies, c, mt, upper_circuit=uc, frame_bars=fb,
                              lower_circuit=lc)
                   for sym, bars, pc, c, mt, uc, fb, lc in jobs())

    trades, events, signals, invalidated, expired, g9_blocked = [], [], 0, 0, 0, 0
    try:
        for n, result in enumerate(results, 1):
            events.extend(result.events)
            signals += result.signals
            invalidated += result.invalidated
            expired += result.expired
            g9_blocked += result.g9_blocked
            trades.extend(result.trades)
            if n % 25 == 0:
                logger.info("[BACKTEST] replayed %d/%d days — %d trades", n, len(days), len(trades))
    finally:
        if procs is not None:
            procs.shutdown(cancel_futures=True)

    frame = pd.DataFrame([t.row(costs) for t in trades])
    summary = {"strategies": [s.name for s in strategies],
               "from": args.start.isoformat(), "to": args.end.isoformat(),
               "symbols_scanned": len(symbols), "min_gain_pct": args.min_gain,
               "gainer_days": len(days), "bands": bands,
               "signals": signals, "gate_invalidated": invalidated,
               "gate_expired": expired, "g9_blocked": g9_blocked,
               "frame": f"last {args.frame_bars} bars" if args.frame_bars else "session",
               **summarise(trades, costs),
               # Requests that failed after every retry: their symbols or days are missing.
               "history_failures": len(history.failures),
               "slippage_pct_per_fill": costs.slippage_pct, "cost_pct_round_trip": costs.cost_pct}
    return frame, summary, pd.DataFrame(events)


def main(argv=None) -> int:
    force_data_only()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    quiet_engine_logs()
    args = parse(argv)
    if args.login:
        connect()
        print("Logged in. Today's token is saved; backtests run without a prompt until it expires.")
        return 0
    if args.end < args.start:
        raise SystemExit("--to is before --from")
    frame, summary, signals = run(args, connect())

    names = args.strategies.replace(",", "+") if args.strategies else "active"
    out = args.out or str(REPORTS_DIR / "backtest" / f"{names}_{args.start}_{args.end}.csv")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    frame.to_csv(out, index=False)
    with open(out.replace(".csv", "_summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    signals.to_csv(out.replace(".csv", "_signals.csv"), index=False)
    print(json.dumps(summary, indent=2, default=str))
    print(f"\nTrades: {out}")
    if not args.no_report:
        print(f"Dashboard: {report.build(out, leverage=args.leverage)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
