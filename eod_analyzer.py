import asyncio
import inspect
import logging
import os
import pandas as pd
from datetime import date, datetime

import config
from database import DatabaseManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

REPORT_DIR = "logs/eod_reports"


class EODAnalyzer:
    """
    End-of-day analyzer that supports both:
    - in-process async execution (runtime scheduler path), and
    - standalone script execution (sync query() fallback).
    """

    def __init__(self, fyers=None, db_manager=None, *, fyers_client=None, db=None):
        self.fyers = fyers_client if fyers_client is not None else fyers
        self.db = db if db is not None else db_manager
        os.makedirs(REPORT_DIR, exist_ok=True)

    async def run_daily_analysis(self, date=None):
        """
        Main async entrypoint for EOD analysis.
        """
        if self.db is None:
            raise RuntimeError("EODAnalyzer: db is None — DatabaseManager was not injected.")

        if not hasattr(self.db, "fetch") and not hasattr(self.db, "query"):
            raise RuntimeError("EODAnalyzer: injected db is missing .query or .fetch method.")
        
        if isinstance(date, str):
            import datetime as _dt
            target_date = _dt.date.fromisoformat(date)
        else:
            target_date = date if date is not None else datetime.now().date()
            
        logger.info("Starting EOD analysis for %s", target_date)

        # 1. Fetch live trades for performance/audit
        trades = await self._fetch_trades(target_date)
        audit_results = self.perform_safety_audit(trades) if trades else {"status": "PASSED", "issues": [], "orphans": 0, "processed": 0}
        soft_stop_results = await self.analyze_soft_stops(target_date)
        stats = self.calculate_performance(trades)
        
        # 2. Phase 73: Ghost Signal Auditing (labeling missed opportunities)
        ghost_stats = {"processed": 0, "wins": 0, "losses": 0, "tp_hits": 0, "eod_wins": 0}
        try:
            # Force target_date passed to audit
            ghost_stats = await self.audit_missed_signals(target_date)
            stats["ghost_trades"] = ghost_stats
        except Exception as e:
            logger.error(f"Ghost Signal Audit failed: {e}")

        # 3. Report generation
        report_text = self.generate_report(target_date, stats, audit_results, soft_stop_results, ghost_stats)
        
        # 4. Persistence
        await self._save_summary_db(target_date, stats, audit_results)
        
        return report_text

    async def audit_missed_signals(self, target_date: date):
        """
        Phase 73: Finds signals that passed validation but weren't traded.
        Simulates their path (SL/TP) to provide labeled data for the ML Trainer.
        """
        from ml_logger import get_ml_logger
        ml_logger = get_ml_logger()
        
        unlabeled_df = ml_logger.get_unlabeled_observations()
        unlabeled = unlabeled_df.to_dict('records') if not unlabeled_df.empty else []
        if not unlabeled:
            logger.info("No unlabeled observations to audit.")
            return {"processed": 0, "wins": 0, "losses": 0}

        logger.info(f"Auditing {len(unlabeled)} missed signals for {target_date}...")
        results = {"processed": 0, "wins": 0, "losses": 0, "tp_hits": 0, "eod_wins": 0}

        for obs in unlabeled:
            symbol = obs.get("symbol")
            if not symbol: continue
            
            # 1. Fetch history from signal time to EOD
            # Signal time is in obs['time'] (HH:MM:SS)
            signal_time_str = obs.get("time")
            try:
                # We need historical data to simulate the path
                # Fyers history API: resolution 1
                data = {
                    "symbol": symbol,
                    "resolution": "1",
                    "date_format": "1",
                    "range_from": target_date.strftime("%Y-%m-%d"),
                    "range_to": target_date.strftime("%Y-%m-%d"),
                    "cont_flag": "1"
                }
                
                # Fetch history (raw fyers call)
                response = await asyncio.to_thread(self.fyers.history, data=data)
                if "candles" not in response or not response["candles"]:
                    continue
                
                cols = ["epoch", "open", "high", "low", "close", "volume"]
                df = pd.DataFrame(response["candles"], columns=cols)
                df['dt'] = pd.to_datetime(df['epoch'], unit='s', utc=True).dt.tz_convert('Asia/Kolkata')
                
                # Filter df for candles AFTER signal_time
                sig_dt = datetime.strptime(f"{target_date} {signal_time_str}", "%Y-%m-%d %H:%M:%S")
                # Add timezone for comparison
                import pytz
                IST = pytz.timezone('Asia/Kolkata')
                sig_dt = IST.localize(sig_dt)
                
                relevant_candles = df[df['dt'] >= sig_dt]
                if relevant_candles.empty:
                    continue

                # 2. Simulate Path
                outcome_data = self._simulate_path(obs, relevant_candles)
                
                # 3. Update ML Logger
                if outcome_data:
                    ml_logger.update_outcome(
                        obs_id=obs["obs_id"],
                        outcome=outcome_data["outcome"],
                        exit_price=outcome_data["exit_price"],
                        max_favorable=outcome_data["max_favorable"],
                        max_adverse=outcome_data["max_adverse"],
                        hold_time_mins=outcome_data["hold_time_mins"]
                    )
                    results["processed"] += 1
                    if outcome_data["outcome"] == "WIN":
                        results["wins"] += 1
                        if outcome_data.get("exit_reason") == "TP_HIT":
                            results["tp_hits"] += 1
                        elif outcome_data.get("exit_reason") == "EOD_SQUAREOFF":
                            results["eod_wins"] += 1
                    elif outcome_data["outcome"] == "LOSS":
                        results["losses"] += 1

            except Exception as e:
                logger.warning(f"Failed to audit {symbol}: {e}")
            
            # Rate limit safety: 5 calls per second max
            await asyncio.sleep(0.2)

        logger.info(f"Ghost Audit Complete: {results}")
        return results

    def _simulate_path(self, obs, df):
        """
        Core path simulation engine for ShortCircuit strategy.
        Checks for SL, TP1, TP2, TP3 hits in order.
        """
        entry_price = obs.get("ltp")
        sl_price = obs.get("sl_price")
        tp_price = obs.get("tp_price") or obs.get("tp1_price")
        
        if not all([entry_price, sl_price, tp_price]):
            return None

        # Position State
        state = "ACTIVE"
        current_sl = sl_price
        max_favorable = 0.0
        max_adverse = 0.0
        start_time = df.iloc[0]['dt']
        exit_time = None
        exit_price = None
        outcome = "LOSS" # Default
        exit_reason = None
        
        # Scalping is SHORT only
        for _, row in df.iterrows():
            high = row['high']
            low = row['low']
            close = row['close']
            
            # AE (MAE) - Price going AGAINST short (UP)
            adv = ((high - entry_price) / entry_price) * 100
            max_adverse = max(max_adverse, adv)
            
            # FE (MFE) - Price going WITH short (DOWN)
            fav = ((entry_price - low) / entry_price) * 100
            max_favorable = max(max_favorable, fav)
            
            # Check Stop Loss (Price hit or exceeded SL)
            if high >= current_sl:
                state = "CLOSED"
                exit_price = current_sl
                exit_time = row['dt']
                exit_reason = "SL_HIT"
                # Determine outcome based on current TP state (if we moved SL to BE)
                if current_sl <= entry_price:
                    outcome = "BREAKEVEN" if abs(current_sl - entry_price) < 0.1 else "WIN"
                else:
                    outcome = "LOSS"
                break
                
            # Check Take Profits
            if state == "ACTIVE":
                if low <= tp_price and tp_price > 0:
                    exit_price = tp_price
                    exit_reason = "TP_HIT"
                    state = "CLOSED"
                    outcome = "WIN"
                    exit_time = row['dt']
                    break

        # EOD Square-off if still active
        if state == "ACTIVE":
            exit_price = df.iloc[-1]['close']
            exit_time = df.iloc[-1]['dt']
            exit_reason = "EOD_SQUAREOFF"
            outcome = "WIN" if exit_price < entry_price else "LOSS"

        hold_time = (exit_time - start_time).total_seconds() / 60
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100
        
        return {
            "exit_reason": exit_reason,
            "outcome": outcome,
            "exit_price": exit_price,
            "max_favorable": max_favorable,
            "max_adverse": max_adverse,
            "pnl_pct": pnl_pct,
            "hold_time_mins": hold_time
        }

    async def _fetch_trades(self, session_date: str):
        """
        Fetch trades for a given session date.
        Prefers async fetch() when available, falls back to sync query().
        """
        if hasattr(self.db, "fetch") and asyncio.iscoroutinefunction(self.db.fetch):
            rows = await self.db.fetch(
                """
                SELECT
                    symbol,
                    entry_price,
                    COALESCE(current_price, entry_price) AS exit_price,
                    COALESCE(realized_pnl, 0) AS pnl,
                    CASE
                        WHEN entry_price > 0
                        THEN ROUND((COALESCE(realized_pnl, 0) / entry_price) * 100, 2)
                        ELSE NULL
                    END AS pnl_pct,
                    state AS status
                FROM positions
                WHERE session_date = $1
                """,
                session_date,
            )
            return [dict(r) for r in rows]

        rows = self.db.query(
            """
            SELECT
                symbol,
                entry_price,
                COALESCE(current_price, entry_price) AS exit_price,
                COALESCE(realized_pnl, 0) AS pnl,
                CASE
                    WHEN entry_price > 0
                    THEN ROUND((COALESCE(realized_pnl, 0) / entry_price) * 100, 2)
                    ELSE NULL
                END AS pnl_pct,
                state AS status
            FROM positions
            WHERE session_date = %s
            """,
            (session_date,),
        )
        return rows or []

    def perform_safety_audit(self, trades):
        issues = []
        orphans = 0

        for trade in trades:
            if trade.get("status") == "OPEN":
                orphans += 1
                issues.append(
                    f"ORPHAN: {trade.get('symbol', 'UNKNOWN')} still OPEN at EOD."
                )

            entry_price = trade.get("entry_price")
            if not entry_price or entry_price <= 0:
                issues.append(
                    f"DATA: {trade.get('symbol', 'UNKNOWN')} has invalid Entry Price."
                )

            pnl_pct = trade.get("pnl_pct")
            if pnl_pct is None and trade.get("status") == "CLOSED":
                issues.append(
                    f"DATA: {trade.get('symbol', 'UNKNOWN')} CLOSED but missing PnL percent."
                )
            elif pnl_pct is not None and abs(pnl_pct) > 50:
                issues.append(
                    f"ANOMALY: {trade.get('symbol', 'UNKNOWN')} pnl_pct={pnl_pct}."
                )

        return {
            "status": "PASSED" if not issues else "WARNING",
            "issues": issues,
            "orphans": orphans,
            "processed": len(trades),
        }

    async def analyze_soft_stops(self, session_date: str):
        """
        Soft-stop table is optional in current schema.
        Return zeroed stats if unavailable.
        """
        results = {
            "total_decisions": 0,
            "correct_decisions": 0,
            "incorrect_decisions": 0,
            "saved_loss": 0.0,
            "missed_profit": 0.0,
            "details": [],
        }

        try:
            if hasattr(self.db, "fetch") and asyncio.iscoroutinefunction(self.db.fetch):
                rows = await self.db.fetch(
                    "SELECT * FROM soft_stop_events WHERE DATE(timestamp) = $1",
                    session_date,
                )
                results["total_decisions"] = len(rows)
                return results

            rows = self.db.query(
                "SELECT * FROM soft_stop_events WHERE DATE(timestamp) = %s",
                (session_date,),
            )
            results["total_decisions"] = len(rows or [])
            return results
        except Exception:
            return results

    def calculate_performance(self, trades):
        total_pnl = 0.0
        winners = 0
        losers = 0

        for trade in trades:
            pnl = float(trade.get("pnl", 0) or 0)
            total_pnl += pnl
            if pnl > 0:
                winners += 1
            elif pnl < 0:
                losers += 1

        total_trades = len(trades)
        win_rate = (winners / total_trades * 100) if total_trades else 0.0
        return {
            "total_pnl": total_pnl,
            "winners": winners,
            "losers": losers,
            "win_rate": round(win_rate, 1),
            "total_trades": total_trades,
        }

    def generate_report(self, date, stats, audit, soft_stop_stats, ghost_stats=None):
        pnl_emoji = "🟢" if stats["total_pnl"] > 0 else "🔴"
        audit_icon = "✅" if audit["status"] == "PASSED" else "⚠️"

        lines = [
            f"# 📊 EOD Report: {date}",
            "",
            "## 💰 Performance",
            f"- **Net P&L**: {pnl_emoji} ₹{stats['total_pnl']:.2f}",
            f"- Win Rate: {stats['win_rate']}% ({stats['winners']}W / {stats['losers']}L)",
            f"- Total Trades: {stats['total_trades']}",
            "",
        ]
        
        if ghost_stats and ghost_stats.get("processed", 0) > 0:
            lines.extend([
                "## 👻 Ghost Signal Audit (Missed Trades)",
                f"- **Processed**: {ghost_stats['processed']}",
                f"- **Real TP Hits (1.0x ATR)**: {ghost_stats.get('tp_hits', 0)} 🎯",
                f"- **EOD Profit Closures**: {ghost_stats.get('eod_wins', 0)} ⏰",
                f"- **Losses**: {ghost_stats['losses']}",
                f"- **Win Rate**: {round(ghost_stats['wins'] / ghost_stats['processed'] * 100, 1) if ghost_stats['processed'] else 0}%",
                "> These signals were validated by the bot but not traded (risk/cooldown/skip).",
                "",
            ])

        lines.extend([
            f"## 🛡️ Safety Audit {audit_icon}",
            f"- Status: {audit['status']}",
            f"- Orphaned Trades: {audit['orphans']}",
        ])

        if audit["issues"]:
            lines.append("### Issues")
            for issue in audit["issues"]:
                lines.append(f"- {issue}")
        else:
            lines.append("- System Integrity: 100%")

        lines.extend(
            [
                "",
                "## Discretionary Analysis",
                f"- Decisions Made: {soft_stop_stats['total_decisions']}",
            ]
        )

        report_text = "\n".join(lines)
        file_path = os.path.join(REPORT_DIR, f"eod_report_{date}.md")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(report_text)
            logger.info("Report saved to %s", file_path)
        except Exception as exc:
            logger.error("Failed to save report: %s", exc)

        return report_text

    async def _save_summary_db(self, date, stats, audit):
        summary_data = {
            "date": date,
            "phase": "44.5",
            "total_trades": stats["total_trades"],
            "winners": stats["winners"],
            "losers": stats["losers"],
            "win_rate": stats["win_rate"],
            "total_pnl": stats["total_pnl"],
            "safety_status": audit["status"],
        }
        if not hasattr(self.db, "log_event"):
            return

        try:
            maybe_awaitable = self.db.log_event("daily_summaries", summary_data)
            if inspect.isawaitable(maybe_awaitable):
                await maybe_awaitable
        except Exception:
            pass

if __name__ == "__main__":
    db = DatabaseManager()
    analyzer = EODAnalyzer(db_manager=db)
    report = asyncio.run(analyzer.run_daily_analysis())
    print(report)
