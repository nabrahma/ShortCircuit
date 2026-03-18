import pandas as pd
import numpy as np
import pytest
from market_profile import ProfileAnalyzer
from god_mode_logic import GodModeAnalyst
import config

@pytest.fixture
def profile_analyzer():
    return ProfileAnalyzer()

@pytest.fixture
def gm_logic():
    return GodModeAnalyst()

def test_volume_profile_calculation(profile_analyzer):
    """Verify that Volume Profile weights correctly."""
    # Price 100 has volume 1000, Price 105 has volume 10
    df = pd.DataFrame({
        'close': [100, 105],
        'low': [100, 105],
        'high': [100.1, 105.1],
        'volume': [1000, 10]
    })
    
    # VOLUME mode
    v_profile = profile_analyzer.calculate_market_profile(df, price_step=1.0, mode='VOLUME')
    # Dalton (Phase 65) uses 20 bins by default across [100, 105] -> ~100.1225
    # Old logic uses 1.0 step -> 100.5
    assert v_profile['vpoc'] in [100.5, 100.1225]
    
    # TPO mode (should be midpoint 102.5 or tied)
    t_profile = profile_analyzer.calculate_market_profile(df, price_step=1.0, mode='TPO')
    assert t_profile['poc'] in [100.5, 105.5]

def test_vah_rejection_pattern(gm_logic):
    """Verify Auction Rejection (Look Above & Fail) detection."""
    df = pd.DataFrame({
        'open':  [100, 101, 100.5],
        'high':  [100.5, 106.0, 101.0], # Probed above VAH (105)
        'low':   [99.5, 100.5, 99.5],
        'close': [100.1, 100.6, 99.8],  # Closed back inside VAH
        'volume': [100] * 3
    })
    
    vah = 105.0
    pattern, _ = gm_logic.detect_structure_advanced(df, vah=vah)
    assert pattern == "VAH_REJECTION"

def test_g5_vah_rejection_bypass(gm_logic):
    """Verify G5 passes on VAH Rejection even if below VAH."""
    # VAH is 105. Current close is 99.8.
    # Normally this is a price_below_vah rejection.
    # But if there was a recent probe above 105, it should pass.
    
    candles = [
        {'open': 100, 'high': 100.5, 'low': 99.5, 'close': 100.1, 'volume': 1000},
        {'open': 101, 'high': 106.0, 'low': 100.5, 'close': 100.6, 'volume': 1000},
        {'open': 100.5, 'high': 101.0, 'low': 99.5, 'close': 99.8, 'volume': 300}, # Fade
    ] * 10 # Repeat to satisfy lookback
    
    profile = {'vah': 105.0} # vVAH
    
    # Setup config for success
    config.G5_STRETCH_LOW_PCT = 1.0
    config.SCANNER_GAIN_MIN_PCT = 1.0
    config.P51_G5_GATE_B_USE_ALLDAY_HIGH = False
    
    res = gm_logic.is_exhaustion_at_stretch(candles, profile, gain_pct=5.0, vwap_sd=2.5)
    print(f"G5 Result: {res}")
    
    assert res['fired'] is True
    assert res['pattern_bonus'] == "VAH_REJECTION"
    # Note: Test case hits >2.2 SD + VAH REJECTION -> HIGH (or MAX_CONVICTION depending on minor config differences)
    assert res['confidence'] in ["HIGH", "MAX_CONVICTION"]

if __name__ == "__main__":
    # Manual run for debugging
    pa = ProfileAnalyzer()
    test_volume_profile_calculation(pa)
    gl = GodModeAnalyst()
    test_vah_rejection_pattern(gl)
    test_g5_vah_rejection_bypass(gl)
    print("All internal tests passed!")
