import pytest
import asyncio
import datetime
import pytz
from unittest.mock import MagicMock, patch, AsyncMock
from focus_engine import FocusEngine
import config

@pytest.fixture
def focus_engine():
    # Mocking FyersConnect to avoid real authentication attempt
    with patch('focus_engine.FyersConnect') as mock_conn:
        mock_conn.return_value.authenticate.return_value = MagicMock()
        
        # Mocking engines
        om = MagicMock()
        om.enter_position = AsyncMock(return_value={'status': 'SUCCESS', 'trade_id': 'T123', 'entry': 99.5, 'qty': 10})
        om.is_exec_cooldown_active = MagicMock(return_value=(False, 0))
        om.capital = MagicMock()
        om.capital.is_slot_free = True
        
        fe = FocusEngine(order_manager=om)
        fe.fyers = MagicMock()
        fe.telegram_bot = MagicMock()
        fe.telegram_bot.is_auto_mode = MagicMock(return_value=True)
        fe.telegram_bot.send_alert = AsyncMock()
        fe.telegram_bot.queue_signal_validation_update = AsyncMock()
        
        fe.start_focus = MagicMock()
        
        fe.analyzer = MagicMock()
        fe.analyzer.signal_manager = MagicMock()
        fe.analyzer.signal_manager.can_signal = MagicMock(return_value=(True, ""))
        
        return fe

@pytest.mark.asyncio
async def test_g12_wick_survival_and_close_entry(focus_engine):
    """
    Phase 58 Logic Test:
    Verify that a wick above invalidation price does NOT kill the signal,
    and a close below trigger price DOES execute the signal.
    """
    IST = pytz.timezone('Asia/Kolkata')
    # Use a fixed morning time (10:00 AM IST) to avoid EOD Guard (15:10)
    now = datetime.datetime(2026, 3, 20, 10, 0, 0, tzinfo=IST)
    current_min = now.replace(second=0, microsecond=0)
    expected_ts = int(current_min.timestamp()) - 60
    
    symbol = "WickStock"
    signal_data = {
        'symbol': symbol,
        'signal_low': 100.0,
        'signal_high': 105.0,
        'correlation_id': 'corr_123'
    }
    
    # Config setup
    config.P58_G12_USE_CANDLE_CLOSE = True
    config.P51_ENABLED = True
    config.P51_G12_INVALIDATION_BUFFER_PCT = 0.002 # 0.2%
    
    # Add signal to gate
    focus_engine.add_pending_signal(signal_data)
    pending = focus_engine.pending_signals[symbol]
    
    # Invalidation price will be 105.0 * 1.002 = 105.21
    # Trigger price will be 100.0
    assert pending['invalidate'] == 105.0 * 1.002
    assert pending['trigger'] == 100.0
    
    # --- STEP 1: TEST WICK SURVIVAL ---
    # Mock history to return a candle where High > Invalidation but Close < Trigger
    # Candle format: [epoch, open, high, low, close, volume]
    wick_candle = [
        expected_ts, 
        101.0, 
        106.0, # High is 106.0 (Greater than 105.21)
        100.5, 
        100.5, # Close is 100.5 (Greater than 100.0 - not triggered yet)
        5000
    ]
    focus_engine.fyers.history = MagicMock(return_value={'s': 'ok', 'candles': [wick_candle]})
    
    # Step 1: Run with mocked time (10:00 AM IST)
    mock_now = current_min + datetime.timedelta(seconds=5)
    with patch('datetime.datetime') as mock_dt:
        mock_dt.now.return_value = mock_now
        mock_dt.now.side_effect = None # ensure it returns mock_now
        mock_dt.timedelta = datetime.timedelta
        
        # Run iteration
        await focus_engine.check_pending_signals(None)
    
        # Verify:
        # 1. Signal NOT deleted (should survive the wick since it didn't CLOSE above 105.21)
        assert symbol in focus_engine.pending_signals
        # 2. last_evaluated_minute should be set
        assert focus_engine.pending_signals[symbol]['last_evaluated_minute'] == current_min
        # 3. Enter position NOT called
        assert focus_engine.order_manager.enter_position.call_count == 0
        
        # --- STEP 2: TEST CLOSE ENTRY ---
        # Move time forward by 1 minute
        new_min = current_min + datetime.timedelta(minutes=1)
        new_expected_ts = int(new_min.timestamp()) - 60
        
        # Mock history to return a candle that closes below trigger
        # Candle format: [epoch, open, high, low, close, volume]
        entry_candle = [
            new_expected_ts, 
            100.5, 
            101.0, 
            99.5, 
            99.5, # Close is 99.5 (Less than 100.0 - TRIGGERED!)
            6000
        ]
        focus_engine.fyers.history = MagicMock(return_value={'s': 'ok', 'candles': [entry_candle]})
        
        # Update mock time for Step 2
        mock_dt.now.return_value = new_min + datetime.timedelta(seconds=5)
            
        # Run iteration
        await focus_engine.check_pending_signals(None)
    
    # Verify:
    # 1. Enter position WAS called
    assert focus_engine.order_manager.enter_position.call_count == 1
    # 2. Signal deleted from pending
    assert symbol not in focus_engine.pending_signals
