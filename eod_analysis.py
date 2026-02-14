"""
Phase 41.2: End-of-Day Analysis Script
Usage: python eod_analysis.py

Loads today's signals from CSV, fetches post-signal price history,
and simulates both legacy and scalper systems side by side.
"""

import os
import sys
import logging

import pandas as pd
from datetime import datetime

import config
from trade_simulator import TradeSimulator

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

SIGNALS_CSV = "logs/signals.csv"
EOD_SUMMARY_CSV = "logs/eod_summary.csv"


class EODAnalyzer:
    """Compare legacy vs scalper risk systems on today's signals."""

    def __init__(self):
        self.simulator = TradeSimulator()
        self.fyers = self._init_fyers()

    def _init_fyers(self):
        """Init Fyers connection (reuses existing auth)."""
        try:
            from fyers_connect import FyersConnect
            conn = FyersConnect()
            return conn.authenticate()
        except Exception as e:
            logger.error(f"Fyers auth failed: {e}")
            logger.info("Running in offline mode — provide price history manually.")
            return None

    def load_todays_signals(self, date_str: str = None) -> list:
        """
        Load signals from CSV for a specific date.

        Args:
            date_str: Date string 'YYYY-MM-DD'. Defaults to today.

        Returns:
            List of signal dicts
        """
        if not os.path.exists(SIGNALS_CSV):
            print(f"No signals file found at {SIGNALS_CSV}")
            return []

        df = pd.read_csv(SIGNALS_CSV)
        df["timestamp"] = pd.to_datetime(df["timestamp"])

        target_date = date_str or datetime.now().strftime("%Y-%m-%d")
        target_date = pd.to_datetime(target_date).date()

        todays = df[df["timestamp"].dt.date == target_date]

        if todays.empty:
            print(f"No signals found for {target_date}")
            return []

        signals = []
        for _, row in todays.iterrows():
            sig = {
                "symbol": row["symbol"],
                "entry_price": float(row["ltp"]),
                "setup_high": float(row.get("setup_high", row["ltp"] * 1.005)),
                "tick_size": float(row.get("tick_size", 0.05)),
                "atr": float(row.get("atr", row["ltp"] * 0.01)),
                "quantity": int(config.CAPITAL / float(row["ltp"])),
                "timestamp": row["timestamp"],
                "pattern": row.get("pattern", ""),
                "stop_loss": float(row["stop_loss"]),
            }
            signals.append(sig)

        return signals

    def fetch_price_history(self, symbol: str, from_time, to_time) -> pd.DataFrame:
        """
        Fetch 1-min candles from signal time to EOD.

        Args:
            symbol: NSE:SYMBOL-EQ
            from_time: datetime (signal generation time)
            to_time: datetime (15:30 or now)

        Returns:
            DataFrame with OHLCV or None
        """
        if self.fyers is None:
            logger.warning("No Fyers connection — cannot fetch history")
            return None

        data = {
            "symbol": symbol,
            "resolution": "1",
            "date_format": "1",
            "range_from": from_time.strftime("%Y-%m-%d"),
            "range_to": to_time.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }

        try:
            response = self.fyers.history(data=data)
            if response.get("s") == "ok" and "candles" in response:
                df = pd.DataFrame(
                    response["candles"],
                    columns=["timestamp", "open", "high", "low", "close", "volume"],
                )
                df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s")

                # Filter to only candles AFTER signal time
                if isinstance(from_time, pd.Timestamp):
                    from_time_naive = from_time.tz_localize(None) if from_time.tzinfo else from_time
                else:
                    from_time_naive = from_time

                df = df[df["timestamp"] >= from_time_naive]
                return df if len(df) > 0 else None
            else:
                logger.warning(f"No history data for {symbol}")
                return None
        except Exception as e:
            logger.error(f"Error fetching history for {symbol}: {e}")
            return None

    def analyze_all_signals(self, date_str: str = None):
        """Main analysis: Simulate both systems on all signals."""

        signals = self.load_todays_signals(date_str)
        target_date = date_str or datetime.now().strftime("%Y-%m-%d")

        if not signals:
            return

        print(f"\n{'='*80}")
        print(f"EOD ANALYSIS: {target_date}")
        print(f"{'='*80}")
        print(f"\nTotal signals generated: {len(signals)}\n")

        results = []

        for idx, signal in enumerate(signals, 1):
            sym = signal["symbol"]
            print(f"\n--- Signal #{idx}: {sym} ---")
            print(f"Entry: ₹{signal['entry_price']:.2f} @ {signal['timestamp']}")

            # Fetch price history from signal time to EOD
            signal_time = signal["timestamp"]
            if isinstance(signal_time, str):
                signal_time = pd.to_datetime(signal_time)

            eod_time = signal_time.replace(hour=15, minute=30, second=0)

            price_df = self.fetch_price_history(sym, signal_time, eod_time)

            if price_df is None or len(price_df) < 2:
                print("  ❌ Insufficient price history, skipping")
                continue

            # Run simulation
            comparison = self.simulator.compare_systems(signal, price_df)
            legacy = comparison["legacy"]
            scalper = comparison["scalper"]

            print(f"\n  LEGACY SYSTEM (Phase 41.1):")
            print(f"    Exit: ₹{legacy['exit']:.2f} ({legacy['exit_reason']})")
            print(f"    P&L: {legacy['pnl_pct']*100:+.2f}% (₹{legacy['pnl_cash']:+.2f})")
            print(f"    Breakeven: {'YES' if legacy['breakeven_hit'] else 'NO'}")
            print(f"    Trailing: {'YES' if legacy['trailing_hit'] else 'NO'}")

            print(f"\n  SCALPER SYSTEM (Phase 41.2):")
            print(f"    Exit: {scalper['exit']} ({scalper['exit_reason']})")
            print(f"    P&L: {scalper['pnl_pct']*100:+.2f}% (₹{scalper['pnl_cash']:+.2f})")
            print(f"    Breakeven: {'YES' if scalper['breakeven_hit'] else 'NO'}")
            print(f"    TP1: {'YES' if scalper['tp1_hit'] else 'NO'}  |  "
                  f"TP2: {'YES' if scalper['tp2_hit'] else 'NO'}  |  "
                  f"TP3: {'YES' if scalper['tp3_hit'] else 'NO'}")

            better_emoji = "🟢" if comparison["better_system"] == "SCALPER" else "🔴"
            print(f"\n  {better_emoji} WINNER: {comparison['better_system']} "
                  f"(Δ {comparison['delta_pnl_pct']*100:+.2f}%, "
                  f"₹{comparison['delta_pnl_cash']:+.2f})")

            results.append(comparison)

        if not results:
            print("\nNo signals could be simulated.")
            return

        # ── Summary ──────────────────────────────────────────────────────
        print(f"\n{'='*80}")
        print(f"SUMMARY STATISTICS")
        print(f"{'='*80}\n")

        legacy_total = sum(r["legacy"]["pnl_cash"] for r in results)
        scalper_total = sum(r["scalper"]["pnl_cash"] for r in results)
        legacy_wins = sum(1 for r in results if r["legacy"]["pnl_cash"] > 0)
        scalper_wins = sum(1 for r in results if r["scalper"]["pnl_cash"] > 0)
        scalper_better = sum(1 for r in results if r["better_system"] == "SCALPER")
        n = len(results)

        print(f"Legacy System (Phase 41.1):")
        print(f"  Total P&L: ₹{legacy_total:+.2f}")
        print(f"  Win Rate:  {legacy_wins}/{n} ({legacy_wins/n*100:.1f}%)")
        print(f"  Avg P&L:   ₹{legacy_total/n:+.2f}\n")

        print(f"Scalper System (Phase 41.2):")
        print(f"  Total P&L: ₹{scalper_total:+.2f}")
        print(f"  Win Rate:  {scalper_wins}/{n} ({scalper_wins/n*100:.1f}%)")
        print(f"  Avg P&L:   ₹{scalper_total/n:+.2f}\n")

        net = scalper_total - legacy_total
        imp_pct = ((net / abs(legacy_total)) * 100) if abs(legacy_total) > 0 else 0
        print(f"Comparison:")
        print(f"  Scalper outperformed: {scalper_better}/{n} signals")
        print(f"  Net improvement:     ₹{net:+.2f}")
        print(f"  Improvement %:       {imp_pct:+.1f}%")

        # Save to CSV
        self._save_summary(target_date, n, legacy_total, scalper_total,
                           legacy_wins, scalper_wins, scalper_better, net)

    def _save_summary(self, date, n, leg_pnl, sca_pnl, leg_wins, sca_wins, sca_better, net):
        """Append daily summary to CSV."""
        os.makedirs(os.path.dirname(EOD_SUMMARY_CSV), exist_ok=True)
        file_exists = os.path.exists(EOD_SUMMARY_CSV)

        summary = pd.DataFrame([{
            "date": date,
            "signals_count": n,
            "legacy_total_pnl": round(leg_pnl, 2),
            "scalper_total_pnl": round(sca_pnl, 2),
            "legacy_win_rate": round(leg_wins / n * 100, 1) if n > 0 else 0,
            "scalper_win_rate": round(sca_wins / n * 100, 1) if n > 0 else 0,
            "scalper_better_count": sca_better,
            "net_improvement": round(net, 2),
        }])

        summary.to_csv(EOD_SUMMARY_CSV, mode="a", header=not file_exists, index=False)
        print(f"\n✓ Results saved to {EOD_SUMMARY_CSV}")
        print(f"\n{'='*80}\n")


if __name__ == "__main__":
    # Accept optional date argument: python eod_analysis.py 2026-02-15
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None

    analyzer = EODAnalyzer()
    analyzer.analyze_all_signals(date_str=date_arg)
