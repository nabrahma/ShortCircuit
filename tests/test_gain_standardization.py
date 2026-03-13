import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from analyzer import FyersAnalyzer

def test_gain_mismatch_standardization():
    """
    Test Case: ZYDUSWELL scenario
    Prev Close: 100
    Gap Down Open: 96
    LTP: 115
    
    Old Logic (vs Open): ((115-96)/96)*100 = 19.79% -> REJECTED (>15%)
    New Logic (vs PC): ((115-100)/100)*100 = 15.00% -> PASSED 
    """
    mock_fyers = MagicMock()
    
    # Mock cache snapshot with Prev Close
    mock_fyers.get_quote_cache_snapshot.return_value = {
        'NSE:ZYDWELL-EQ': {'pc': 100.0, 'ltp': 115.0}
    }
    
    analyzer = FyersAnalyzer(mock_fyers)
    
    # Create simple DF starting at Open=96
    df = pd.DataFrame([
        {'epoch': 1710321000, 'open': 96.0, 'high': 96.0, 'low': 96.0, 'close': 96.0, 'volume': 1000},
        {'epoch': 1710321060, 'open': 96.0, 'high': 115.0, 'low': 96.0, 'close': 115.0, 'volume': 2000}
    ])
    
    # We patch check_constraints to see what gain_pct it receives
    with patch('god_mode_logic.GodModeAnalyst.check_constraints') as mock_check:
        mock_check.return_value = (True, "PASSED")
        
        # 1. Bypass G7 Market Regime
        with patch('market_context.MarketContext.evaluate_g7') as mock_g7:
            mock_g7.return_value = (True, "OK - TEST")
            
            # Set config to some high limit to allow 15% but reject 19%
            with patch('config.RVOL_VALIDITY_GATE_ENABLED', False):
                # analyzer.check_setup calls self.gm_analyst.check_constraints(ltp, day_high, gain_pct, ...)
                analyzer.check_setup('NSE:ZYDWELL-EQ', ltp=115.0, pre_fetched_df=df)
            
            # The 3rd argument to check_constraints should be 15.0
            args, kwargs = mock_check.call_args
            actual_gain = args[2]
            
            print(f"DEBUG: Actual Gain passed to gate: {actual_gain}")
            assert abs(actual_gain - 15.0) < 0.01
            assert actual_gain < 19.0

if __name__ == "__main__":
    test_gain_mismatch_standardization()
