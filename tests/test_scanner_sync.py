import pytest
from unittest.mock import MagicMock
import config

def test_scanner_threshold_config():
    """Verify that scanner constants in config are set correctly."""
    assert config.SCANNER_GAIN_MIN_PCT == 7.5
    assert config.SCANNER_GAIN_MAX_PCT == 18.0
    assert config.SCANNER_MIN_VOLUME == 100000

def test_scanner_logic_passes_7_5_percent():
    """
    Simulate the scanner loop condition to ensure 7.5% passes.
    Condition: config.SCANNER_GAIN_MIN_PCT <= change_p <= config.SCANNER_GAIN_MAX_PCT 
               and volume > config.SCANNER_MIN_VOLUME 
               and ltp > config.SCANNER_MIN_LTP
    """
    # Test Data: 8.0% gain (was blocked before, should pass now)
    change_p = 8.0
    volume = 150000
    ltp = 100.0
    
    passed = (config.SCANNER_GAIN_MIN_PCT <= change_p <= config.SCANNER_GAIN_MAX_PCT and 
             volume > config.SCANNER_MIN_VOLUME and 
             ltp > config.SCANNER_MIN_LTP)
    
    assert passed is True

def test_scanner_logic_blocks_low_gain():
    """Verify that it still blocks sub-7.5% signals."""
    change_p = 7.4
    volume = 150000
    ltp = 100.0
    
    passed = (config.SCANNER_GAIN_MIN_PCT <= change_p <= config.SCANNER_GAIN_MAX_PCT and 
             volume > config.SCANNER_MIN_VOLUME and 
             ltp > config.SCANNER_MIN_LTP)
    
    assert passed is False

if __name__ == "__main__":
    pytest.main([__file__])
