import pandas as pd
import pytest
from unittest.mock import MagicMock
from analyzer import FyersAnalyzer
import config

@pytest.fixture
def analyzer():
    fyers = MagicMock()
    market_context = MagicMock()
    return FyersAnalyzer(fyers)

def test_slope_decay_allowance(analyzer):
    # Setup DF with 31 candles
    df = pd.DataFrame({
        'volume': [1000] * 31,
        'close': [100] * 31,
        'high': [100] * 31,
        'low': [99] * 31,
        'open': [100] * 31
    })
    
    # Mocking config
    config.P57_G4_SLOPE_DECAY_ENABLED = True
    config.P51_G4_SLOPE_MIN = 3.0
    config.P57_G4_DIVERGENCE_SD = 1.5
    
    # CASE 1: Slope above threshold, but NOT decaying
    # slope_now = 5.0, slope_prev = 4.0
    # Should BLOCK
    slope_now = 5.0
    slope_prev = 4.0
    vwap_sd = 2.0
    blocked = analyzer._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, "TEST")
    assert blocked is True
    
    # CASE 2: Slope above threshold, IS decaying, but NOT extended enough
    # slope_now = 5.0, slope_prev = 6.0, vwap_sd = 1.0 (threshold 1.5)
    # Should BLOCK
    slope_now = 5.0
    slope_prev = 6.0
    vwap_sd = 1.0
    blocked = analyzer._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, "TEST")
    assert blocked is True
    
    # CASE 3: Slope above threshold, IS decaying, AND IS extended
    # slope_now = 5.0, slope_prev = 6.0, vwap_sd = 2.0 (threshold 1.5)
    # Should ALLOW (return False)
    slope_now = 5.0
    slope_prev = 6.0
    vwap_sd = 2.0
    blocked = analyzer._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, "TEST")
    assert blocked is False

def test_absorption_relaxation(analyzer):
    # Mocking config
    config.P57_G5_Z_EXTREME_THRESHOLD = 3.3
    config.P57_G5_Z_FADE_RELAXATION = 0.95
    
    # Case: Normal fade ratio (0.8) > 0.65
    # Should block by default
    candles = [
        {'volume': 1000, 'open': 100, 'close': 100.1, 'high': 100.2, 'low': 99.9}
    ] * 20
    # Current candle (last)
    candles[-1] = {'volume': 800, 'open': 100, 'close': 100.1, 'high': 100.2, 'low': 99.9} # fade = 800/1000 = 0.8
    
    # Case A: Not extended, No doji. Should return fired=False.
    res = analyzer.gm_analyst.is_exhaustion_at_stretch(candles, {}, 10.0, 1.0, vwap_sd=2.0)
    assert res['fired'] is False
    assert "volume_not_faded" in res['reject_reason']
    
    # Case B: Extended (3.5 SD), Doji body (< 0.05% body). Should RELAX and return fired=True?
    # body = 100.01 - 100.0 = 0.01. body_pct = 0.01/100 = 0.0001 (< 0.0005)
    candles[-1] = {'volume': 900, 'open': 100, 'close': 100.01, 'high': 100.1, 'low': 99.9} # fade = 0.9
    # We need to satisfy Gate A/B/D as well to get fired=True, or just check the reject_reason.
    
    # Setup for success in Gate A (gain) and B (day high)
    config.SCANNER_GAIN_MIN_PCT = 9.0
    config.G5_STRETCH_LOW_PCT = 9.0
    config.G5_STRETCH_HIGH_PCT = 15.0
    config.P51_G5_GATE_B_USE_ALLDAY_HIGH = False # bypass Gate B for simplicity
    config.P51_G5_GATE_E_LATE_SESSION_EXTREME_ONLY = False # bypass late session rule
    
    # Mock Gate D (profile)
    # It checks profile['vah']. We need ltp > vah.
    # Current LTP is 100.01. 
    profile = {'vah': 95.0}
    
    # Case C: Extended + Absorption Doji. Fade 0.9 allowed.
    res = analyzer.gm_analyst.is_exhaustion_at_stretch(candles, profile, 10.0, 1.0, vwap_sd=3.5)
    assert res['fired'] is True # Should pass volume check now!
