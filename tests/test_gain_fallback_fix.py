import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
from analyzer import FyersAnalyzer

def test_gain_fallback_pc_missing():
    """
    Test Case: NSE:GUJALKALI-EQ scenario (missing PC)
    Prev Close in Cache: 0 (Missing)
    ch_oc in Cache: 13.12 (Direct Gain from Broker)
    Open Price: 500.0
    LTP: 490.0
    
    Old Logic: ((490.0-500.0)/500.0)*100 = -2.0% -> REJECTED
    New Logic: pc is missing, use ch_oc=13.12 -> PASSED
    """
    mock_fyers = MagicMock()
    
    # scenario 2: pc missing but ch_oc available
    mock_fyers.get_quote_cache_snapshot.return_value = {
        'NSE:GUJALKALI-EQ': {'pc': 0.0, 'ch_oc': 13.12, 'ltp': 490.0}
    }
    
    analyzer = FyersAnalyzer(fyers=mock_fyers, broker=mock_fyers)
    
    # Create simple DF starting at Open=500
    df = pd.DataFrame([
        {'epoch': 1710321000, 'open': 500.0, 'high': 500.0, 'low': 500.0, 'close': 500.0, 'volume': 1000},
        {'epoch': 1710321060, 'open': 500.0, 'high': 500.0, 'low': 490.0, 'close': 490.0, 'volume': 2000}
    ])
    
    with patch('god_mode_logic.GodModeAnalyst.check_constraints') as mock_check:
        mock_check.return_value = (True, "PASSED")
        with patch('market_context.MarketContext.evaluate_g7') as mock_g7:
            mock_g7.return_value = (True, "OK - TEST")
            with patch('config.RVOL_VALIDITY_GATE_ENABLED', False):
                analyzer.check_setup('NSE:GUJALKALI-EQ', ltp=490.0, pre_fetched_df=df)
            
            args, _ = mock_check.call_args
            actual_gain = args[2]
            
            print(f"DEBUG: Actual Gain passed to gate: {actual_gain}")
            # Should match exactly 13.12
            assert abs(actual_gain - 13.12) < 0.001

def test_gain_fallback_full_missing():
    """
    Test Case: No PC, No ch_oc in cache
    Open Price: 500.0
    LTP: 490.0
    
    Should fall back to Open Price baseline -> -2.0%
    """
    mock_fyers = MagicMock()
    mock_fyers.get_quote_cache_snapshot.return_value = {}
    
    analyzer = FyersAnalyzer(fyers=mock_fyers, broker=mock_fyers)
    
    df = pd.DataFrame([
        {'epoch': 1710321000, 'open': 500.0, 'high': 500.0, 'low': 500.0, 'close': 500.0, 'volume': 1000},
        {'epoch': 1710321060, 'open': 500.0, 'high': 500.0, 'low': 490.0, 'close': 490.0, 'volume': 2000}
    ])
    
    with patch('god_mode_logic.GodModeAnalyst.check_constraints') as mock_check:
        mock_check.return_value = (True, "PASSED")
        with patch('market_context.MarketContext.evaluate_g7') as mock_g7:
            mock_g7.return_value = (True, "OK - TEST")
            with patch('config.RVOL_VALIDITY_GATE_ENABLED', False):
                analyzer.check_setup('NSE:TEST-EQ', ltp=490.0, pre_fetched_df=df)
            
            args, _ = mock_check.call_args
            actual_gain = args[2]
            
            # (490-500)/500 * 100 = -2.0
            assert abs(actual_gain - (-2.0)) < 0.001

if __name__ == "__main__":
    pytest.main([__file__])
