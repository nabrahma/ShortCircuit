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
    """Phase 86: Verify G1 retrace softening from 1.5% to 3.0% on IMMEDIATE decay detection.
    
    Murphy: Momentum divergence (slope declining) IS the confirmation.
    Leung & Li: Optimal entry is when reversion force is active.
    No timer needed — slope_now < slope_prev + extended = immediate decay.
    """
    symbol = "TEST_SYM"
    
    # Setup data: 100 candles trending up to ~102
    prices = np.linspace(90, 102, 100)
    df = pd.DataFrame({
        'high': prices + 0.1,
        'low': prices - 0.1,
        'close': prices,
        'open': prices - 0.05,
        'volume': np.ones(100) * 1000
    })
    df['vwap'] = df['close'].expanding().mean()
    
    # Ensure config is enabled
    monkeypatch.setattr(config, 'P66_ADAPTIVE_G1_ENABLED', True)
    monkeypatch.setattr(config, 'P66_G4_DECAY_SD_THRESHOLD', 2.5)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR', True)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR_FIXED_PCT', 0.015)
    monkeypatch.setattr(config, 'RVOL_VALIDITY_GATE_ENABLED', False)

    # ── CASE 1: No decay (slope accelerating) ──────────────────
    # slope_now=12 > slope_prev=10 → NOT decaying → Kill Backdoor stays at 1.5%
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(12.0, "T"), (10.0, "T")] * 10)
    analyzer.gm_analyst.calculate_vwap_bands = MagicMock(return_value=3.0)  # Extended
    
    # LTP 100.0, day_high ~102.1 → retrace 2.06% > 1.5% → REJECT
    res = analyzer.check_setup(symbol, 100.0, pre_fetched_df=df)
    assert res is None  # Rejected by G1 Kill Backdoor (1.5% threshold, no decay)

    # ── CASE 2: Decay detected IMMEDIATELY (slope declining + extended) ────
    # slope_now=8 < slope_prev=12 → DECAYING → Kill Backdoor softens to 3.0%
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(8.0, "T"), (12.0, "T")] * 10)
    analyzer.gm_analyst.calculate_vwap_bands = MagicMock(return_value=3.0)  # Extended at +3 SD
    
    # Mock remaining gates to pass
    analyzer.market_context.evaluate_g7 = MagicMock(return_value=(True, "OK"))
    analyzer.gm_analyst.is_exhaustion_at_stretch = MagicMock(return_value={
        "fired": True, "stretch_score": 1.0, "vol_fade_ratio": 0.2, 
        "confidence": "HIGH", "pattern_bonus": "None"
    })
    
    # LTP 100.0, day_high ~102.1 → retrace 2.06% — WITHIN 3.0% softened threshold
    res = analyzer.check_setup(symbol, 100.0, pre_fetched_df=df)
    # Should pass G1 now (decay softened threshold to 3.0%)
    assert res is not None
    assert res['symbol'] == symbol
    # Signal High should be the snapshot high (~102.1)
    assert res['signal_high'] > 102.0
    # Stop loss should be above peak_high
    assert res['stop_loss'] > res['signal_high']

def test_no_decay_when_not_extended(analyzer, monkeypatch):
    """Decay should NOT activate when VWAP SD is below threshold, even if slope is declining."""
    symbol = "TEST_SYM2"
    
    prices = np.linspace(90, 102, 100)
    df = pd.DataFrame({
        'high': prices + 0.1,
        'low': prices - 0.1,
        'close': prices,
        'open': prices - 0.05,
        'volume': np.ones(100) * 1000
    })
    df['vwap'] = df['close'].expanding().mean()
    
    monkeypatch.setattr(config, 'P66_ADAPTIVE_G1_ENABLED', True)
    monkeypatch.setattr(config, 'P66_G4_DECAY_SD_THRESHOLD', 2.5)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR', True)
    monkeypatch.setattr(config, 'P51_G1_KILL_BACKDOOR_FIXED_PCT', 0.015)
    monkeypatch.setattr(config, 'RVOL_VALIDITY_GATE_ENABLED', False)

    # slope_now=8 < slope_prev=12 → slope IS declining
    # BUT vwap_sd=1.5 < threshold 2.5 → NOT extended enough
    analyzer.gm_analyst.calculate_vwap_slope = MagicMock(side_effect=[(8.0, "T"), (12.0, "T")] * 10)
    analyzer.gm_analyst.calculate_vwap_bands = MagicMock(return_value=1.5)  # Not extended
    
    # LTP 100.0, day_high ~102.1 → retrace 2.06% > 1.5% → should REJECT (no decay without extension)
    res = analyzer.check_setup(symbol, 100.0, pre_fetched_df=df)
    assert res is None  # Still rejected — decay needs both slope decline AND extension
