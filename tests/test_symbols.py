import unittest
import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from symbols import validate_symbol, NIFTY_50, format_stock_symbol

class TestSymbols(unittest.TestCase):
    
    def test_nifty_symbol_format(self):
        """Test NIFTY symbol is valid"""
        self.assertTrue(validate_symbol(NIFTY_50))
        self.assertEqual(NIFTY_50, 'NSE:NIFTY50-INDEX')
    
    def test_validation(self):
        """Test symbol validation"""
        # Valid
        self.assertTrue(validate_symbol('NSE:SBIN-EQ'))
        self.assertTrue(validate_symbol('NSE:NIFTY50-INDEX'))
        
        # Invalid
        self.assertFalse(validate_symbol('SBIN'))
        self.assertFalse(validate_symbol('NSE:SBIN')) # Missing hyphen/type
        self.assertFalse(validate_symbol(''))
    
    def test_format_stock_symbol(self):
        """Test symbol formatting"""
        self.assertEqual(format_stock_symbol('SBIN'), 'NSE:SBIN-EQ')
        self.assertEqual(format_stock_symbol('NSE:SBIN-EQ'), 'NSE:SBIN-EQ')

if __name__ == '__main__':
    unittest.main()
