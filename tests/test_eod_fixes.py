
import unittest
import sys
import os
from unittest.mock import MagicMock

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from eod_analyzer import EODAnalyzer

class TestEODFixes(unittest.TestCase):
    def setUp(self):
        self.mock_fyers = MagicMock()
        self.mock_db = MagicMock()
        self.analyzer = EODAnalyzer(self.mock_fyers, self.mock_db)

    def test_safety_audit_none_pnl(self):
        """Test safety audit with None pnl_pct"""
        trades = [
            {
                'symbol': 'TEST',
                'status': 'OPEN',
                'entry_price': 100,
                'pnl_pct': None # This caused crash
            },
            {
                'symbol': 'TEST2', 
                'status': 'CLOSED',
                'entry_price': 100,
                'pnl_pct': None # Should flag as missing data
            },
            {
                'symbol': 'TEST3',
                'status': 'CLOSED',
                'entry_price': 100,
                'pnl_pct': 60.0 # Should flag as anomaly
            }
        ]
        
        result = self.analyzer.perform_safety_audit(trades)
        
        # Check no crash
        self.assertIsNotNone(result)
        
        issues = result['issues']
        print("\nAudit Issues Found:")
        for i in issues:
            print(f"- {i}")
            
        # Verify specific issues
        # TEST2 should have missing PnL warning
        self.assertTrue(any('TEST2' in i and 'missing PnL' in i for i in issues))
        
        # TEST3 should have anomaly warning
        self.assertTrue(any('TEST3' in i and 'ANOMALY' in i for i in issues))
        
        # TEST should be orphan (OPEN)
        self.assertTrue(any('TEST' in i and 'ORPHAN' in i for i in issues))

if __name__ == '__main__':
    unittest.main()
