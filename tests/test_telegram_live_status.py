import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from telegram import Update, Message, Chat, User
from telegram.ext import ContextTypes
from telegram_bot import ShortCircuitBot

@pytest.mark.asyncio
async def test_cmd_status_live_sync():
    # Setup mocks
    order_manager = MagicMock()
    order_manager.get_today_trades = AsyncMock(return_value=[])
    
    capital_manager = MagicMock()
    capital_manager.sync = AsyncMock()
    capital_manager.get_real_time_capital = MagicMock(return_value=1800.0)
    capital_manager.get_available_margin = MagicMock(return_value=9000.0)
    
    config = {
        'TELEGRAM_BOT_TOKEN': 'fake_token',
        'TELEGRAM_CHAT_ID': '12345'
    }
    
    bot = ShortCircuitBot(config, order_manager, capital_manager)
    bot._auto_mode = True
    bot._is_authorized = MagicMock(return_value=True)
    
    # Mock update
    update = MagicMock(spec=Update)
    update.message = AsyncMock(spec=Message)
    update.effective_chat = MagicMock(spec=Chat)
    update.effective_chat.id = 12345
    update.effective_user = MagicMock(spec=User)
    update.effective_user.first_name = "User"
    
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    # Run command
    await bot._cmd_status(update, context)
    
    # Verify sync calls
    capital_manager.sync.assert_called_once()
    order_manager.get_today_trades.assert_called_once()
    
    # Verify response contains "Live"
    args, kwargs = update.message.reply_text.call_args
    text = args[0]
    assert "Live" in text
    assert "₹1800" in text
    assert "HTML" == kwargs.get('parse_mode')

@pytest.mark.asyncio
async def test_cmd_pnl_live_sync():
    # Setup mocks
    order_manager = MagicMock()
    # Mock some trades
    trades = [
        {'symbol': 'NSE:RELIANCE-EQ', 'realised_pnl': 100.0, 'exit_reason': 'TP1_HIT'},
        {'symbol': 'NSE:SBIN-EQ', 'realised_pnl': -50.0, 'exit_reason': 'SL_HIT'}
    ]
    order_manager.get_today_trades = AsyncMock(return_value=trades)
    order_manager.get_active_positions = MagicMock(return_value={})
    
    capital_manager = MagicMock()
    
    config = {
        'TELEGRAM_BOT_TOKEN': 'fake_token',
        'TELEGRAM_CHAT_ID': '12345'
    }
    
    bot = ShortCircuitBot(config, order_manager, capital_manager)
    bot._is_authorized = MagicMock(return_value=True)
    
    # Mock update
    update = MagicMock(spec=Update)
    update.message = AsyncMock(spec=Message)
    
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    # Run command
    await bot._cmd_pnl(update, context)
    
    # Verify sync call
    order_manager.get_today_trades.assert_called_once()
    
    # Verify response
    args, kwargs = update.message.reply_text.call_args
    text = args[0]
    assert "Today's P&L (Live)" in text
    assert "RELIANCE" in text
    assert "₹50.00" in text # Net PnL
    assert "HTML" == kwargs.get('parse_mode')
