import pandas as pd
import numpy as np
import pytest
from unittest.mock import MagicMock
from market_profile import ProfileAnalyzer
from market_context import MarketContext
from analyzer import FyersAnalyzer
import config
from datetime import time, datetime
import pytz

IST = pytz.timezone('Asia/Kolkata')

@pytest.fixture
def profile_analyzer():
    return ProfileAnalyzer()

@pytest.fixture
def market_context():
    fyers = MagicMock()
    return MarketContext(fyers)

@pytest.fixture
def analyzer():
    fyers = MagicMock()
    # Mock quote snapshot for gain calculation
    fyers.get_quote_cache_snapshot.return_value = {}
    return FyersAnalyzer(fyers)

def test_dalton_value_area(profile_analyzer):
    """Verify Dalton 70% Volume Rule."""
    # POC at 110 with 1000 vol. Others have 10.
    # Total Vol = 1000 + 20*10 = 1200. 70% = 840.
    # POC bin alone satisfies 70%.
    closes = np.linspace(100, 120, 100)
    volumes = np.ones(100) * 10
    volumes[45:55] = 1000 # POC in the middle
    
    df = pd.DataFrame({
        'close': closes,
        'high': closes + 0.1,
        'low': closes - 0.1,
        'volume': volumes
    })
    
    profile = profile_analyzer.calculate_dalton_value_area(df, bins=20)
    assert profile is not None
    # With 20 bins for [100, 120], width is 1.0. POC at ~110.
    assert 109 <= profile['poc'] <= 111
    assert profile['vah'] >= profile['poc']
    assert profile['val'] <= profile['poc']
    # Ensure VA is not zero
    assert profile['vah'] > profile['val']

def test_g7_climax_window(market_context, monkeypatch):
    """Verify 09:30 - 10:00 Climax Exception."""
    # Mock time to 09:35 (Inside Climax Window)
    mock_now = datetime.now(IST).replace(hour=9, minute=35, second=0)
    
    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return mock_now
            
    monkeypatch.setattr('market_context.datetime', MockDateTime)
    
    # Case 1: No climax at 09:35 -> Blocked
    allowed, reason = market_context.evaluate_g7(vwap_sd=1.0, profile_rejection=False, volume_z=0.0)
    assert allowed is False
    assert "Opening Window" in reason

    # Case 2: Climax at 09:35 -> Allowed
    allowed, reason = market_context.evaluate_g7(vwap_sd=3.5, profile_rejection=True, volume_z=2.5)
    assert allowed is True
    assert "Climax Exception" in reason

    # Case 3: 09:50 (Now also Climax Window, formerly Normal) -> Blocked if no climax
    mock_now = datetime.now(IST).replace(hour=9, minute=50, second=0)
    allowed, reason = market_context.evaluate_g7(vwap_sd=1.0, profile_rejection=False, volume_z=0.0)
    assert allowed is False
    assert "Opening Window" in reason

def test_g1_soft_threshold(analyzer, monkeypatch):
    """Verify 7.5% - 9.0% gain allowance with AMT."""
    # Mock time to 10:30 (Safe session)
    mock_now = datetime.now(IST).replace(hour=10, minute=30, second=0)
    
    class MockDateTime(datetime):
        @classmethod
        def now(cls, tz=None): return mock_now
            
    import datetime as real_datetime
    class MockDateTimeModule:
        def __init__(self):
            self.datetime = MockDateTime
            self.date = real_datetime.date
            self.time = real_datetime.time
            self.timedelta = real_datetime.timedelta
            self.timezone = real_datetime.timezone

    monkeypatch.setattr('analyzer.datetime', MockDateTimeModule())
    monkeypatch.setattr('market_context.datetime', MockDateTime)

    # Setup history data (100 candles)
    prices = np.linspace(100, 108, 100) # Ends at 108 (8% gain)
    df = pd.DataFrame({
        'close': prices,
        'high': prices + 0.5,
        'low': prices - 0.5,
        'volume': [1000] * 100,
        'open': [100.0] * 100,
        'vwap': prices # dummy vwap
    })
    
    # Baseline 100. LTP 108 (8% gain)
    ltp = 108.0
    
    # 1. Mock profile analyzer
    # The real ProfileAnalyzer.calculate_market_profile(..., mode='VOLUME') 
    # will call calculate_dalton_value_area if P65_AMT_ENABLED=True.
    # In tests, we mock it.
    mock_profile = {
        'poc': 104.0, 'vah': 105.0, 'val': 103.0,
        'vpoc': 104.0, 'vvah': 105.0, 'vval': 103.0
    }
    analyzer.profile_analyzer.calculate_market_profile = MagicMock(return_value=mock_profile)
    
    # 2. Mock G7
    analyzer.market_context.evaluate_g7 = MagicMock(return_value=(True, "OK"))
    analyzer.market_context.get_volume_z_score = MagicMock(return_value=2.5)
    
    # 3. Mock GodModeAnalyst methods
    analyzer.gm_analyst.calculate_atr = MagicMock(return_value=1.5)
    analyzer.gm_analyst.calculate_vwap_bands = MagicMock(return_value=2.5)
    analyzer.gm_analyst.check_constraints = MagicMock(return_value=(True, "OK"))
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(return_value=(0.5, 0.1))
    analyzer.gm_analyst.is_exhaustion_at_stretch = MagicMock(return_value={
        "fired": True, "stretch_score": 3.0, "vol_fade_ratio": 0.5, 
        "confidence": "HIGH", "pattern_bonus": "DOJI", "reject_reason": ""
    })
    
    # 4. Mock other components
    analyzer.market_context.is_circuit_hitter = MagicMock(return_value=False)
    analyzer._check_circuit_guard = MagicMock(return_value=False)
    analyzer._is_momentum_too_strong = MagicMock(return_value=False)
    analyzer._check_pro_confluence = MagicMock(return_value=(True, ["CONF1", "CONF2"]))
    analyzer.signal_manager.can_signal = MagicMock(return_value=(True, ""))
    analyzer.htf_confluence.check_trend_exhaustion = MagicMock(return_value=(True, "HTF_OK"))
    
    # Ensure flag is enabled
    monkeypatch.setattr(config, 'P65_AMT_ENABLED', True)
    monkeypatch.setattr(config, 'P65_G1_NET_GAIN_THRESHOLD', 7.5)
    monkeypatch.setattr(config, 'SCANNER_GAIN_MIN_PCT', 7.5)

    # Mock gate result logger to see failures
    mock_grl = MagicMock()
    def log_fail(gr):
         print(f"GATE_FAIL: {gr.first_fail_gate} | {gr.rejection_reason}")
    mock_grl.record = MagicMock(side_effect=log_fail)
    monkeypatch.setattr('analyzer.get_gate_result_logger', lambda: mock_grl)

    # CASE 1: 8.0% Gain + Profile Rejection -> Should PASS G1 (min_gain=7.5)
    analyzer.profile_analyzer.check_profile_rejection = MagicMock(return_value=(True, "LOOK_ABOVE_FAIL"))
    try:
        res = analyzer.check_setup("TEST_SYMBOL", ltp, pre_fetched_df=df)
    except Exception:
        import traceback
        traceback.print_exc()
        raise
    
    if res is None:
        print("Result is None in Case 1")

    assert res is not None
    assert res.get('tp1_atr_mult_override') == 1.0

    # CASE 2: 8.0% Gain + No Rejection -> Should REJECT G1 (req 9.0)
    analyzer.profile_analyzer.check_profile_rejection = MagicMock(return_value=(False, "VALUE_ACCEPTANCE"))
    res = analyzer.check_setup("TEST_SYMBOL", ltp, pre_fetched_df=df)
    assert res is None # Rejected
