import pytest
from datetime import datetime, timedelta
from signal_manager import SignalManager

def test_cooldown_bypass_logic():
    """
    Test Case: G8.3 Logic Fix
    - Set a discovery cooldown for a symbol.
    - Check can_signal(is_execution=False) -> should be blocked.
    - Check can_signal(is_execution=True) -> should be ALLOWED.
    """
    sm = SignalManager(cooldown_minutes=45)
    symbol = "NSE:BUTTERFLY-EQ"
    
    # Simulate adding to validation gate (sets preemptive cooldown)
    sm.add_pending_signal(symbol)
    
    # 1. Discovery Check (Scanner/Analyzer)
    allowed, reason = sm.can_signal(symbol, is_execution=False)
    print(f"Discovery check: {allowed}, reason: {reason}")
    assert allowed is False
    assert "Cooldown" in reason
    
    # 2. Execution Check (FocusEngine on Trigger)
    allowed, reason = sm.can_signal(symbol, is_execution=True)
    print(f"Execution check: {allowed}, reason: {reason}")
    assert allowed is True
    assert reason == "OK"

def test_hard_block_still_works_during_execution():
    """
    Test Case: Hard block (e.g. Max Session Loss or Exec Cooldown) 
    should STILL block even if is_execution=True.
    """
    sm = SignalManager()
    sm._reset_if_new_day() # Initialize date so manually set is_paused isn't reset
    sm.is_paused = True # Max session loss hit
    symbol = "NSE:ANY-EQ"
    
    allowed, reason = sm.can_signal(symbol, is_execution=True)
    assert allowed is False
    assert "Trading paused" in reason

if __name__ == "__main__":
    pytest.main([__file__])
