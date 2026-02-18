import sys
import os
import unittest
from datetime import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from database import DatabaseManager
from eod_analyzer import EODAnalyzer

class TestEODFlow(unittest.TestCase):
    def setUp(self):
        # Use in-memory DB or temporary file for testing?
        # DatabaseManager uses "data/short_circuit.db" by default.
        # We'll use the real DB logic but maybe a test file if configurable.
        # DatabaseManager hardcodes DB_FILE. 
        # For this test, we'll use the real DB but insert test data with a specific date.
        self.db = DatabaseManager()
        self.test_date = "2099-01-01" # Future date to avoid messing up today's data

    def test_eod_pipeline(self):
        print(f"\n[TEST] Running EOD Pipeline Test for {self.test_date}")
        
        
        # 1. Clean up potential old test data
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM trades WHERE date = ?", (self.test_date,))
        cursor.execute("DELETE FROM soft_stop_events WHERE date = ?", (self.test_date,))
        conn.commit()
        conn.close()

        # 2. Insert Mock Trades
        trade_id = "TEST_TRADE_001"
        self.db.log_trade_entry({
            'trade_id_str': trade_id,
            'date': self.test_date,
            'symbol': 'NSE:TEST-EQ',
            'qty': 50,
            'side': 'BUY',
            'entry_price': 100.0,
            'entry_time': f"{self.test_date} 09:30:00",
            'sl': 98.0,
            'hard_stop': 97.5,
            'strategy_tag': 'TEST_STRAT'
        })
        
        self.db.log_trade_exit(trade_id, {
            'exit_price': 102.0,
            'exit_time': f"{self.test_date} 10:00:00",
            'pnl': 100.0, # (102-100)*50
            'pnl_pct': 2.0,
            'exit_reason': 'TARGET_HIT',
            'status': 'CLOSED'
        })
        
        # 3. Insert Mock Soft Stop Event
        self.db.log_event('soft_stop_events', {
            'trade_id': trade_id,
            'date': self.test_date,
            'symbol': 'NSE:TEST-EQ',
            'soft_stop_decision': 'HOLD',
            'soft_stop_trigger_price': 99.0,
            'outcome': 'Simulated Recovery'
        })
        
        # 4. Run Analyzer
        analyzer = EODAnalyzer(None, self.db) # Fyers is None
        report = analyzer.run_daily_analysis(self.test_date)
        
        print(f"\n[REPORT OUTPUT]\n{report}")
        
        # 5. Verify Report Content
        self.assertIn("# 📊 EOD Report", report)
        self.assertIn("Net P&L", report)
        self.assertIn("Safety Audit", report)
        self.assertIn("Decisions Made: 1", report)
        # Report format checks
        if "No Trades Executed" in report:
            self.fail("Analyzer failed to find inserted trades")
            
        print("[TEST] EOD Pipeline Verified Successfully")

if __name__ == '__main__':
    unittest.main()
