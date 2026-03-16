import pytest
import pandas as pd
import numpy as np
import datetime
from analyzer import FyersAnalyzer
from unittest.mock import MagicMock
import config

@pytest.fixture
def analyzer():
    fyers = MagicMock()
    fyers.get_quote_cache_snapshot.return_value = {}
    return FyersAnalyzer(fyers)

def test_adaptive_g1_retrace_softening(analyzer, monkeypatch):
    """Verify G1 retrace softening from 1.5% to 3.0% on verified decay."""
    symbol = "TEST_SYM"
    
    # Setup data
    prices = np.linspace(90, 102, 100)
    df = pd.DataFrame({
        'high': prices + 0.1,
        'low': prices - 0.1,
        'close': prices,
        'open': prices - 0.05,
        'volume': np.ones(100) * 1000
    })
    df['vwap'] = df['close'].expanding().mean()
    
    # Mock time
    start_time = datetime.datetime(2026, 3, 16, 10, 0, 0)
    
    class TimeState:
        now_val = start_time

    def mock_now(tz=None):
        return TimeState.now_val
            
    # Mock the class entirely to control .now()
    mock_dt_class = MagicMock()
    mock_dt_class.now = mock_now
    monkeypatch.setattr('analyzer.datetime.datetime', mock_dt_class)

    # Ensure config is enabled
    monkeypatch.setattr(config, 'P66_ADAPTIVE_G1_ENABLED', True)
    monkeypatch.setattr(config, 'P66_G4_DECAY_CONFIRMATION_SEC', 120)
    monkeypatch.setattr(config, 'P66_G4_DECAY_SD_THRESHOLD', 2.5)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR', True)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR_FIXED_PCT', 0.015)
    monkeypatch.setattr(config, 'RVOL_VALIDITY_GATE_ENABLED', False)

    # First run (No decay yet)
    # slope_now=12, slope_prev=10
    # side_effect needs 3+ values (2 for pre-calc, 1 for G4 block check if it reaches it)
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(12.0, "T"), (10.0, "T")] * 10)
    analyzer.gm_analyst.calculate_vwap_bands = MagicMock(return_value=3.0) 

    analyzer.check_setup(symbol, 102.0, pre_fetched_df=df)
    assert symbol not in analyzer.slope_decay_tracker

    # 2. Decay Starts (Slope 8, 12)
    # slope_now = 8, slope_prev = 12
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(8.0, "T"), (12.0, "T")] * 10)
    analyzer.check_setup(symbol, 102.0, pre_fetched_df=df)
    assert symbol in analyzer.slope_decay_tracker
    assert analyzer.slope_decay_tracker[symbol] == start_time

    # 3. Retrace 2.0% (LTP 100.0 from High 102.1) - Should REJECT
    TimeState.now_val = start_time + datetime.timedelta(seconds=60)
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(7.0, "T"), (8.0, "T")] * 10)
    res = analyzer.check_setup(symbol, 100.0, pre_fetched_df=df) 
    assert res is None # Still rejected by G1 (1.5% retrace > 1.5% limit)

    # 4. Confirmation Window Met (T+125s)
    TimeState.now_val = start_time + datetime.timedelta(seconds=125)
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(6.0, "T"), (7.0, "T")] * 10)
    
    # Passing other gates
    analyzer.market_context.evaluate_g7 = MagicMock(return_value=(True, "OK"))
    analyzer.gm_analyst.is_exhaustion_at_stretch = MagicMock(return_value={
        "fired": True, "stretch_score": 1.0, "vol_fade_ratio": 0.2, 
        "confidence": "HIGH", "pattern_bonus": "None"
    })
    # G4 passing (decay detected)
    analyzer.gm_analyst.evaluate_g4 = MagicMock(return_value=(True, "OK"))
    # G9 passing
    analyzer.htf_confluence.evaluate_g9 = MagicMock(return_value=(True, "OK"))
    
    res = analyzer.check_setup(symbol, 100.0, pre_fetched_df=df)
    assert res is not None 
    assert res['symbol'] == symbol
    # Signal High should be the snapshot high (~102.1)
    assert res['signal_high'] > 102.0
    # Stop loss should be above peak_high
    assert res['stop_loss'] > res['signal_high']
