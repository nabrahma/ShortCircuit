import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock
from analyzer import FyersAnalyzer
from god_mode_logic import GodModeAnalyst
import config

class MockConfig:
    PHASE_51_ENABLED = True
    P51_G4_RVOL_THRESHOLD = 5.0
    P51_G4_SLOPE_MIN = 3.0
    P57_G4_SLOPE_DECAY_ENABLED = True
    P57_G4_DIVERGENCE_SD = 1.5
    P60_G4_STRUCTURAL_FALLBACK_GAIN = 10.0
    P60_G5_SPEAR_VOL_CLIMAX_MULT = 3.0
    G5_STRETCH_LOW_PCT = 9.0
    G5_STRETCH_HIGH_PCT = 15.0
    SCANNER_GAIN_MIN_PCT = 9.0
    P51_G5_GATE_B_USE_ALLDAY_HIGH = False
    P55_G5_VOL_FADE_LOOKBACK = 5

@pytest.fixture
def mock_analyzer():
    fyers = MagicMock()
    analyzer = FyersAnalyzer(fyers, 100, 90)
    return analyzer

@pytest.fixture
def mock_gm():
    return GodModeAnalyst()

def test_g4_structural_fallback(mock_analyzer, monkeypatch):
    """
    Inject a candidate with gain=12%, vwap_sd=1.2 (below normal 1.5), 
    but slope decaying (now=4.0, prev=6.0).
    Verify it PASSES G4 (returns False for too_strong).
    """
    import sys
    monkeypatch.setitem(sys.modules, "config", MockConfig)
    
    # Mock DF with some volume and data
    df = pd.DataFrame({
        'volume': [100] * 30,
        'close': [100] * 30
    })
    
    slope_now = 4.0
    slope_prev = 6.0
    vwap_sd = 1.2
    symbol = "TEST_STOCK"
    gain_pct = 12.0
    
    # too_strong should be False because of Structural Fallback
    blocked = mock_analyzer._is_momentum_too_strong(df, slope_now, slope_prev, vwap_sd, symbol, gain_pct)
    assert blocked is False

def test_g5_spear_of_exhaustion(mock_gm, monkeypatch):
    """
    Inject a candidate with volume=3.5x average, high > prev_high, and close < midpoint.
    Verify SPEAR_OF_EXHAUSTION is identified and fired=True.
    """
    import sys
    monkeypatch.setitem(sys.modules, "config", MockConfig)
    
    # 7 candles total. 5 for lookback, 1 for current, 1 safety.
    candles = [
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000},
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000},
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000},
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000},
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000},
        {'open': 100, 'high': 101, 'low': 99, 'close': 100, 'volume': 1000}, # Avg = 1000
        {'open': 110, 'high': 115, 'low': 105, 'close': 106, 'volume': 3500} # Climax (3.5x), Rejection (Midpoint is 110), New High (115 > 101)
    ]
    
    profile = {'vah': 105} # Price 106 is above VAH
    vwap_sd = 2.5
    gain_pct = 11.0
    atr = 2.0
    
    # Signature: candles, profile, gain_pct, atr=0, vwap_sd=0
    result = mock_gm.is_exhaustion_at_stretch(
        candles=candles, 
        profile=profile, 
        gain_pct=gain_pct, 
        atr=atr, 
        vwap_sd=vwap_sd
    )
    
    assert result["fired"] is True
    assert result["pattern_bonus"] == "SPEAR_OF_EXHAUSTION"
    assert result["confidence"] == "MAX_CONVICTION"
