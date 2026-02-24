import asyncio
import inspect
import logging
import os
from datetime import datetime

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
        if not hasattr(self.db, "query") or not callable(self.db.query):
            raise RuntimeError(
                "EODAnalyzer: DatabaseManager missing .query() method — check interface contract."
            )

        target_date = date or datetime.now().strftime("%Y-%m-%d")
        logger.info("Starting EOD analysis for %s", target_date)

        trades = await self._fetch_trades(target_date)
        if not trades:
            logger.info("No trades found for analysis.")
            return "No Trades Executed Today."

        audit_results = self.perform_safety_audit(trades)
        soft_stop_results = await self.analyze_soft_stops(target_date)
        stats = self.calculate_performance(trades)
        report_text = self.generate_report(target_date, stats, audit_results, soft_stop_results)

        await self._save_summary_db(target_date, stats, audit_results)
        self._generate_session_log(target_date)
        return report_text

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

    def generate_report(self, date, stats, audit, soft_stop_stats):
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
            f"## 🛡️ Safety Audit {audit_icon}",
            f"- Status: {audit['status']}",
            f"- Orphaned Trades: {audit['orphans']}",
        ]

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

    def _generate_session_log(self, date):
        log_path = getattr(config, "LOG_FILE", "logs/bot.log")
        output_path = "md/terminal_log.md"
        date_str = str(date)

        if not os.path.exists(log_path):
            logger.warning("Log file not found at %s", log_path)
            return

        try:
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            matches = []
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.startswith(date_str):
                        matches.append(line.rstrip())

            with open(output_path, "w", encoding="utf-8") as f:
                f.write("# ShortCircuit Session Log\n")
                f.write(f"> **Date:** {date_str}\n\n")
                if matches:
                    f.write(f"Total log entries: {len(matches)}\n\n")
                    f.write("```log\n")
                    for line in matches:
                        f.write(line + "\n")
                    f.write("```\n")
                else:
                    f.write(f"No log entries found for {date_str}.\n")
        except Exception as exc:
            logger.error("Failed to generate session log: %s", exc)


if __name__ == "__main__":
    db = DatabaseManager()
    analyzer = EODAnalyzer(db_manager=db)
    report = asyncio.run(analyzer.run_daily_analysis())
    print(report)
