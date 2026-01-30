import unittest
from unittest.mock import MagicMock, patch
import pandas as pd
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from market_scanner import MarketScanner

class TestScanner(unittest.TestCase):
    def setUp(self):
        self.mock_kite = MagicMock()
        self.scanner = MarketScanner(self.mock_kite)

    def test_scanner_pump_filter(self):
        # Mock Universe
        self.scanner.get_universe = MagicMock(return_value=['TEST_STOCK'])
        
        # Mock Quote Response
        # Condition: LTP 100, PrevClose 85 (Change +17.6%) -> PASS
        # Volume: 1M -> Turnover 100M -> PASS
        mock_quote = {
            'NSE:TEST_STOCK': {
                'last_price': 100.0,
                'ohlc': {'close': 85.0},
                'volume': 1000000,
                'upper_circuit_limit': 110.0 # 10% away -> SAFE
            }
        }
        self.mock_kite.quote.return_value = mock_quote
        
        # Mock Earnings (Safe)
        self.scanner.check_earnings = MagicMock(return_value=True)
        
        candidates = self.scanner.scan()
        self.assertIn('TEST_STOCK', candidates)

    def test_scanner_trap_filter(self):
        # Mock Universe
        self.scanner.get_universe = MagicMock(return_value=['TRAP_STOCK'])
        
        # Condition: LTP 100, UC 101 (Dist 1% < 1.5%) -> FAIL
        mock_quote = {
            'NSE:TRAP_STOCK': {
                'last_price': 100.0,
                'ohlc': {'close': 85.0},
                'volume': 1000000,
                'upper_circuit_limit': 101.0
            }
        }
        self.mock_kite.quote.return_value = mock_quote
        
        self.scanner.check_earnings = MagicMock(return_value=True)
        
        candidates = self.scanner.scan()
        self.assertNotIn('TRAP_STOCK', candidates)

if __name__ == '__main__':
    unittest.main()
