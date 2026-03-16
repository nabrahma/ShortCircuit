import unittest
from unittest.mock import Mock, patch
import sys
import os
import datetime

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from scanner import FyersScanner
import config

class TestScannerBlacklist(unittest.TestCase):
    
    def setUp(self):
        self.mock_fyers = Mock()
        self.scanner = FyersScanner(self.mock_fyers)
        
    @patch('config.minutes_since_market_open')
    @patch('config.RVOL_VALIDITY_GATE_ENABLED', True)
    @patch('config.RVOL_MIN_CANDLES', 45)
    def test_rvol_gate_does_not_blacklist(self, mock_mins):
        """Verify that RVOL gate skip doesn't increment blacklist counter."""
        symbol = "NSE:SBIN-EQ"
        
        # 1. Simulate early morning (10 mins since open)
        mock_mins.return_value = 10.0
        
        # Mock history to return something valid but we expect skip before it's even processed
        self.mock_fyers.history.return_value = {'s': 'ok', 'candles': [[0]*6]*50}
        
        # Call check_chart_quality 5 times
        for _ in range(5):
            is_good, df, df_15m = self.scanner.check_chart_quality(symbol)
            self.assertFalse(is_good)
            
        # Verify blacklist counter is 0 (BUG: currently it will be 5)
        # Note: In the bugged version, it increments quality_reject_counts[symbol]
        reject_count = self.scanner.quality_reject_counts.get(symbol, 0)
        self.assertEqual(reject_count, 0, f"RVOL gate skip should not increment blacklist. Got {reject_count}")

    def test_insufficient_data_does_not_blacklist(self):
        """Verify that insufficient candle data doesn't increment blacklist counter."""
        symbol = "NSE:RELIANCE-EQ"
        
        # Mock history to return empty candles
        self.mock_fyers.history.return_value = {'s': 'ok', 'candles': []}
        
        # Ensure we pass the RVOL gate if enabled
        with patch('config.RVOL_VALIDITY_GATE_ENABLED', False):
            # Call check_chart_quality 5 times
            for _ in range(5):
                is_good, df, df_15m = self.scanner.check_chart_quality(symbol)
                self.assertFalse(is_good)
                
            # Verify blacklist counter is 0 (BUG: currently it will be 5)
            reject_count = self.scanner.quality_reject_counts.get(symbol, 0)
            self.assertEqual(reject_count, 0, f"Insufficient data skip should not increment blacklist. Got {reject_count}")

    def test_zero_volume_DOES_blacklist(self):
        """Verify that zero volume correctly increments blacklist counter (existing good behavior)."""
        symbol = "NSE:JUNK-EQ"
        
        # Mock history to return 100% zero volume candles
        candles = []
        for i in range(20):
            candles.append([i * 60, 100, 105, 95, 102, 0]) # volume = 0
            
        self.mock_fyers.history.return_value = {'s': 'ok', 'candles': candles}
        
        # Ensure we pass the RVOL gate
        with patch('config.RVOL_VALIDITY_GATE_ENABLED', False):
            # Call check_chart_quality 2 times
            for _ in range(2):
                is_good, df, df_15m = self.scanner.check_chart_quality(symbol)
                self.assertFalse(is_good)
                
            # Verify blacklist counter is 2
            reject_count = self.scanner.quality_reject_counts.get(symbol, 0)
            self.assertEqual(reject_count, 2)

if __name__ == '__main__':
    unittest.main()
