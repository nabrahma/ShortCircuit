import unittest
from datetime import time, datetime
from unittest.mock import Mock, patch
import sys
import os

# Add parent dir
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from market_session import MarketSession, IST

class TestMarketSession(unittest.TestCase):
    
    def setUp(self):
        self.mock_fyers = Mock()
        self.mock_telegram = Mock()
        self.session = MarketSession(self.mock_fyers, self.mock_telegram)
    
    def test_state_detection_premarket(self):
        """Test PRE_MARKET state detection"""
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 17, 8, 30, tzinfo=IST)
            state = self.session.get_current_state()
            self.assertEqual(state, 'PRE_MARKET')
    
    def test_state_detection_early(self):
        """Test EARLY_MARKET state detection"""
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 17, 9, 20, tzinfo=IST)
            state = self.session.get_current_state()
            self.assertEqual(state, 'EARLY_MARKET')
    
    def test_state_detection_mid(self):
        """Test MID_MARKET state detection"""
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 17, 11, 30, tzinfo=IST)
            state = self.session.get_current_state()
            self.assertEqual(state, 'MID_MARKET')
    
    def test_state_detection_eod(self):
        """Test EOD_WINDOW state detection"""
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 17, 15, 15, tzinfo=IST)
            state = self.session.get_current_state()
            self.assertEqual(state, 'EOD_WINDOW')
    
    def test_state_detection_post(self):
        """Test POST_MARKET state detection"""
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 17, 16, 0, tzinfo=IST)
            state = self.session.get_current_state()
            self.assertEqual(state, 'POST_MARKET')
    
    def test_weekend_detection(self):
        """Test weekend is POST_MARKET"""
        # Saturday
        with patch('market_session.datetime') as mock_datetime:
            mock_datetime.now.return_value = datetime(2026, 2, 21, 10, 0, tzinfo=IST)  # Saturday
            state = self.session.get_current_state()
            self.assertEqual(state, 'POST_MARKET')
    
    def test_next_market_open_weekday(self):
        """Test next market open on Friday (should be Monday)"""
        with patch('market_session.datetime') as mock_datetime:
            # Friday 4 PM
            mock_datetime.now.return_value = datetime(2026, 2, 20, 16, 0, tzinfo=IST)
            
            # Pass through combine to real implementation
            mock_datetime.combine.side_effect = lambda d, t, tzinfo=None: datetime.combine(d, t, tzinfo)
            
            next_open = self.session._next_market_open_time()
            
            # Should be Monday 9:15 AM
            self.assertEqual(next_open.weekday(), 0)  # Monday
            self.assertEqual(next_open.time(), time(9, 15))

if __name__ == '__main__':
    unittest.main()
