import pytest
from unittest.mock import MagicMock
import pandas as pd
from htf_confluence import HTFConfluence
import config

class MockFyers:
    def history(self, data):
        return {"s": "ok", "candles": []}

@pytest.fixture
def htf():
    return HTFConfluence(MockFyers())

def test_g9_bypass_extreme_stretch(htf):
    # SD = 3.5 (> 3.0 threshold)
    allowed, msg = htf.check_trend_exhaustion("TEST", vwap_sd=3.5)
    assert allowed is True
    assert "Alpha Strike" in msg

def test_g9_block_momentum_acceleration(htf):
    # SD = 2.0 (below bypass)
    # Current move = +2.5% (> 2.0% threshold)
    # Prev move = +1.0%
    # Accel = 1.5%
    df_15m = pd.DataFrame({
        'c': [100, 101, 103.525] # 103.525 is ~2.5% up from 101
    })
    # Wait, let's just mock exact values
    # prev_c = 100
    # curr_c = 102.5 (+2.5%)
    # pprev_c = 99
    # prev_move = (100-99)/99 = 1.01%
    # curr_move = (102.5-100)/100 = 2.5%
    df_15m = pd.DataFrame({
        'c': [99, 100, 102.5] 
    })
    
    allowed, msg = htf.check_trend_exhaustion("TEST", df_15m=df_15m, vwap_sd=2.0)
    assert allowed is False
    assert "Momentum Accel" in msg

def test_g9_pass_momentum_stall(htf):
    # SD = 2.0
    # Current move = +0.5% (< 1.0% threshold)
    # prev_c = 100
    # curr_c = 100.5 (+0.5%)
    # pprev_c = 98
    # prev_move = 2%
    df_15m = pd.DataFrame({
        'c': [98, 100, 100.5]
    })
    
    allowed, msg = htf.check_trend_exhaustion("TEST", df_15m=df_15m, vwap_sd=2.0)
    assert allowed is True
    assert "Momentum Stall" in msg

def test_g9_block_sustained_trend(htf):
    # SD = 2.0
    # Current move = 1.5% (between Stall and Accel)
    df_15m = pd.DataFrame({
        'c': [98, 99, 100.485] # 100.485 is ~1.5% up from 99
    })
    
    allowed, msg = htf.check_trend_exhaustion("TEST", df_15m=df_15m, vwap_sd=2.0)
    assert allowed is False
    assert "Sustained Trend" in msg
