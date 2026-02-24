import unittest
import asyncio

from eod_analyzer import EODAnalyzer


class FakeDB:
    def __init__(self):
        self._trades = {}
        self._soft_stop_events = []
        self.daily_summaries = []

    def log_trade_entry(self, data):
        trade_id = data["trade_id_str"]
        self._trades[trade_id] = {
            "trade_id_str": trade_id,
            "date": data["date"],
            "symbol": data["symbol"],
            "qty": data["qty"],
            "entry_price": data["entry_price"],
            "pnl": 0.0,
            "pnl_pct": None,
            "status": "OPEN",
        }

    def log_trade_exit(self, trade_id, data):
        trade = self._trades[trade_id]
        trade["pnl"] = data.get("pnl", 0.0)
        trade["pnl_pct"] = data.get("pnl_pct")
        trade["status"] = data.get("status", "CLOSED")

    def log_event(self, event_type, details):
        if event_type == "soft_stop_events":
            self._soft_stop_events.append(details)
        elif event_type == "daily_summaries":
            self.daily_summaries.append(details)

    def query(self, query, params):
        date = params[0]
        if "FROM positions" in query:
            return [t for t in self._trades.values() if t["date"] == date]
        if "FROM soft_stop_events" in query:
            return [e for e in self._soft_stop_events if e["date"] == date]
        return []


class TestEODFlow(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.test_date = "2099-01-01"

    def test_eod_pipeline(self):
        trade_id = "TEST_TRADE_001"
        self.db.log_trade_entry(
            {
                "trade_id_str": trade_id,
                "date": self.test_date,
                "symbol": "NSE:TEST-EQ",
                "qty": 50,
                "entry_price": 100.0,
            }
        )

        self.db.log_trade_exit(
            trade_id,
            {
                "exit_price": 102.0,
                "pnl": 100.0,
                "pnl_pct": 2.0,
                "status": "CLOSED",
            },
        )

        self.db.log_event(
            "soft_stop_events",
            {
                "trade_id": trade_id,
                "date": self.test_date,
                "symbol": "NSE:TEST-EQ",
                "soft_stop_decision": "HOLD",
                "soft_stop_trigger_price": 99.0,
            },
        )

        analyzer = EODAnalyzer(None, self.db)
        report = asyncio.run(analyzer.run_daily_analysis(self.test_date))

        self.assertIn("# 📊 EOD Report", report)
        self.assertIn("Net P&L", report)
        self.assertIn("Safety Audit", report)
        self.assertIn("Decisions Made: 1", report)
        self.assertNotIn("No Trades Executed", report)


if __name__ == "__main__":
    unittest.main()
