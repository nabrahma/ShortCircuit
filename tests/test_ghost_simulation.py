import os
import sys
import unittest
import pandas as pd
from datetime import datetime
import pytz

# Add parent directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eod_analyzer import EODAnalyzer

class TestGhostSimulation(unittest.TestCase):
    def setUp(self):
        self.analyzer = EODAnalyzer()
        self.ist = pytz.timezone('Asia/Kolkata')
        self.base_dt = self.ist.localize(datetime(2026, 3, 19, 10, 0, 0))

    def create_mock_df(self, price_path):
        candles = []
        for i, price in enumerate(price_path):
            dt = self.base_dt.replace(minute=i)
            candles.append({
                'epoch': int(dt.timestamp()),
                'open': price,
                'high': price + 0.1,
                'low': price - 0.1,
                'close': price,
                'volume': 1000,
                'dt': dt
            })
        return pd.DataFrame(candles)

    def test_sl_hit(self):
        # Entry at 100, SL at 102, TP at 98
        obs = {
            "ltp": 100.0,
            "sl_price": 102.0,
            "tp_price": 98.0
        }
        # Price path hits SL before any TP
        prices = [100.0, 101.0, 102.5, 95.0]
        df = self.create_mock_df(prices)
        
        result = self.analyzer._simulate_path(obs, df)
        self.assertEqual(result["outcome"], "LOSS")
        self.assertGreaterEqual(result["exit_price"], 102.0)

    def test_full_win_tp(self):
        # Entry 100, TP 94
        obs = {
            "ltp": 100.0,
            "sl_price": 102.0,
            "tp_price": 94.0
        }
        # Path: 100 -> 97 -> 95 -> 93.5
        prices = [100.0, 97.0, 95.0, 93.5]
        df = self.create_mock_df(prices)
        
        result = self.analyzer._simulate_path(obs, df)
        self.assertEqual(result["outcome"], "WIN")
        self.assertEqual(result["exit_price"], 94.0)

    def test_eod_squareoff_win(self):
        obs = {
            "ltp": 100.0,
            "sl_price": 105.0,
            "tp_price": 90.0, # Far away
        }
        # Stayed below entry but never hit TP
        prices = [100.0, 99.0, 98.5, 99.5]
        df = self.create_mock_df(prices)
        
        result = self.analyzer._simulate_path(obs, df)
        self.assertEqual(result["outcome"], "WIN")
        self.assertEqual(result["exit_price"], 99.5)

if __name__ == "__main__":
    unittest.main()
