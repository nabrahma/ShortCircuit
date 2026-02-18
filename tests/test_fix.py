import unittest
from unittest.mock import Mock
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from market_context import MarketContext
from market_session import MarketSession
from symbols import NIFTY_50

class TestFix(unittest.TestCase):
    
    def setUp(self):
        self.mock_fyers = Mock()
        self.mock_bot = Mock()
        
    def test_market_context_symbol(self):
        """Test MarketContext has correct nifty_symbol"""
        mc = MarketContext(self.mock_fyers)
        
        self.assertTrue(hasattr(mc, 'nifty_symbol'))
        self.assertEqual(mc.nifty_symbol, NIFTY_50)
        self.assertEqual(MarketContext.NIFTY_SYMBOL, NIFTY_50)
        
    def test_market_session_symbol(self):
        """Test MarketSession has correct nifty_symbol"""
        ms = MarketSession(self.mock_fyers, self.mock_bot)
        
        # Check instance attribute (if added, I added class attr NIFTY_SYMBOL)
        # Check class attribute
        self.assertTrue(hasattr(MarketSession, 'NIFTY_SYMBOL'))
        self.assertEqual(MarketSession.NIFTY_SYMBOL, NIFTY_50)
        
        # I used self.NIFTY_SYMBOL in _fetch_morning_range, valid access
        self.assertEqual(ms.NIFTY_SYMBOL, NIFTY_50)

if __name__ == '__main__':
    unittest.main()
