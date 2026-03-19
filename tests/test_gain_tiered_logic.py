import pytest
from unittest.mock import MagicMock
import config

# Mocking enough to run the logic check
def simulate_g1_logic(gain_pct, profile_rejection):
    min_gain = config.SCANNER_GAIN_MIN_PCT # 7.5
    normal_threshold = config.P65_G1_NORMAL_THRESHOLD # 9.0
    
    amt_failing_auction = False
    if profile_rejection and gain_pct >= config.P65_G1_NET_GAIN_THRESHOLD: # 7.5
        amt_failing_auction = True

    # Simulation of G1 logic in analyzer.py
    ok = True
    msg = ""
    
    if gain_pct < min_gain:
        ok = False
        msg = f"Insufficient Gain: {gain_pct:.1f}% (need {min_gain}%)"
    elif gain_pct < normal_threshold and not amt_failing_auction:
         ok = False
         msg = f"Low Gain {gain_pct:.1f}% requires AMT Profile Rejection (Failed Auction)"

    return ok, msg

def test_tier_1_normal_pass():
    """9.0%+ should pass without AMT."""
    ok, msg = simulate_g1_logic(9.5, False)
    assert ok is True

def test_tier_2_amt_pass():
    """8.0% should pass IF AMT is present."""
    ok, msg = simulate_g1_logic(8.0, True)
    assert ok is True

def test_tier_2_no_amt_fail():
    """8.0% should FAIL if AMT is missing."""
    ok, msg = simulate_g1_logic(8.0, False)
    assert ok is False
    assert "requires AMT Profile Rejection" in msg

def test_tier_3_below_floor_fail():
    """7.4% should fail regardless of AMT."""
    ok, msg = simulate_g1_logic(7.4, True)
    assert ok is False
    assert "Insufficient Gain" in msg

if __name__ == "__main__":
    pytest.main([__file__])
