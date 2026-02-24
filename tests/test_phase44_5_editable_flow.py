import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from telegram_bot import ShortCircuitBot, SignalMsgState


def _bot():
    return ShortCircuitBot(
        config_settings={
            "TELEGRAM_BOT_TOKEN": "test-token",
            "TELEGRAM_CHAT_ID": "12345",
            "EDITABLE_SIGNAL_FLOW_ENABLED": False,
        },
        order_manager=None,
        capital_manager=None,
        focus_engine=None,
    )


@pytest.mark.asyncio
async def test_start_cleanup_task_creates_and_replaces():
    bot = _bot()

    async def short_loop():
        await asyncio.sleep(0.01)

    bot._cleanup_stale_signal_entries_loop = short_loop
    await bot._start_cleanup_task()
    first_task = bot._cleanup_task
    assert first_task is not None

    old_task = asyncio.create_task(asyncio.sleep(3600))
    bot._cleanup_task = old_task
    await bot._start_cleanup_task()
    assert old_task.cancelled()
    assert bot._cleanup_task is not old_task

    bot._cleanup_task.cancel()
    await asyncio.gather(bot._cleanup_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_stop_null_guard_cleanup_task():
    bot = _bot()
    bot._cleanup_task = None
    bot.app = None
    await bot.stop()


def test_cleanup_single_pass_removes_only_stale():
    bot = _bot()
    bot._signal_msg_index = {
        "fresh": SignalMsgState(created_at=1000.0, message_id=1),
        "stale": SignalMsgState(created_at=500.0, message_id=2),
    }

    removed = bot._cleanup_stale_signal_entries(now=1299.0)
    assert removed == 1
    assert "stale" not in bot._signal_msg_index
    assert "fresh" in bot._signal_msg_index


def test_queue_signal_discovery_non_blocking_no_result_call(monkeypatch):
    bot = _bot()
    bot.app = object()
    bot._loop = SimpleNamespace(is_running=lambda: True)

    class PendingFuture:
        def __init__(self):
            self.result_called = False
            self.callbacks = []

        def add_done_callback(self, callback):
            self.callbacks.append(callback)

        def result(self):
            self.result_called = True
            raise AssertionError("queue_signal_discovery must not call Future.result() inline")

    future = PendingFuture()

    def fake_run_coroutine_threadsafe(coro, loop):
        coro.close()
        return future

    monkeypatch.setattr(
        "telegram_bot.asyncio.run_coroutine_threadsafe",
        fake_run_coroutine_threadsafe
    )

    correlation_id = asyncio.run(bot.queue_signal_discovery({"symbol": "NSE:TEST-EQ", "entry_price": 100.0}))
    assert isinstance(correlation_id, str)
    assert correlation_id in bot._signal_msg_index
    assert future.result_called is False


@pytest.mark.asyncio
async def test_done_callback_discard_after_pop_no_reinsert(monkeypatch):
    bot = _bot()
    bot.app = object()

    gate = asyncio.Event()
    captured_message_id = 999

    async def fake_send_signal_discovery(signal):
        await gate.wait()
        return captured_message_id

    monkeypatch.setattr(bot, "send_signal_discovery", fake_send_signal_discovery)

    correlation_id = await bot.queue_signal_discovery(
        {"symbol": "NSE:RACE-EQ", "entry_price": 100.0}
    )

    with bot._signal_msg_index_lock:
        bot._signal_msg_index.pop(correlation_id, None)

    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with bot._signal_msg_index_lock:
        assert correlation_id not in bot._signal_msg_index


def test_validation_text_renders_with_and_without_details():
    bot = _bot()
    signal = {
        "symbol": "NSE:INFOEDGE-EQ",
        "side": "SHORT",
        "entry_price": 1500.0,
        "stop_loss": 1530.0,
        "target": 1460.0,
        "signal_low": 1498.0,
        "ltp": 1499.0,
    }

    validated_text = bot._build_signal_validation_text(signal, "VALIDATED", None)
    assert "GATE 12 VALIDATED" in validated_text
    assert "Entry:" in validated_text
    assert "Reason:" in validated_text

    rejected_text = bot._build_signal_validation_text(
        signal,
        "REJECTED",
        {"reason": "INVALIDATED_PRE_ENTRY", "ltp": 1531.0, "trigger_price": 1498.0},
    )
    assert "GATE 12 REJECTED" in rejected_text
    assert "INVALIDATED_PRE_ENTRY" in rejected_text

    timeout_text = bot._build_signal_validation_text(
        signal,
        "TIMEOUT",
        {"reason": "VALIDATION_TIMEOUT", "timeout_minutes": 15, "ltp": 1501.0, "trigger_price": 1498.0},
    )
    assert "GATE 12 TIMEOUT" in timeout_text
    assert "15 minute" in timeout_text


@pytest.mark.asyncio
async def test_eod_db_primary_path():
    bot = _bot()
    bot._session_trades = [
        {"symbol": "NSE:SESSION-EQ", "pnl": 10.0, "exit_reason": "SOFT_STOP"}
    ]

    class FakeDB:
        async def get_today_trades(self):
            return [
                {"symbol": "NSE:DB-EQ", "pnl": 125.5, "exit_reason": "TARGET_HIT"},
                {"symbol": "NSE:DB2-EQ", "pnl": -25.0, "exit_reason": "HARD_STOP_FILLED"},
            ]

    bot.order_manager = SimpleNamespace(_target=SimpleNamespace(db=FakeDB()))

    sent_messages = []

    async def _capture_send(text, parse_mode="Markdown", reply_markup=None):
        sent_messages.append(text)
        return None

    bot.send_message = AsyncMock(side_effect=_capture_send)
    await bot.send_eod_summary()

    assert sent_messages, "EOD summary message was not sent"
    payload = sent_messages[-1]
    assert "[DB UNAVAILABLE - SHOWING SESSION DATA ONLY]" not in payload
    assert "NSE:DB-EQ" in payload
    assert "NSE:SESSION-EQ" not in payload


@pytest.mark.asyncio
async def test_eod_db_unavailable_falls_back_to_session():
    bot = _bot()
    bot._session_trades = [
        {"symbol": "NSE:FALLBACK-EQ", "pnl": 35.0, "exit_reason": "MANUAL_EXIT"}
    ]

    class BrokenDB:
        async def get_today_trades(self):
            raise RuntimeError("db down")

    bot.order_manager = SimpleNamespace(_target=SimpleNamespace(db=BrokenDB()))

    sent_messages = []

    async def _capture_send(text, parse_mode="Markdown", reply_markup=None):
        sent_messages.append(text)
        return None

    bot.send_message = AsyncMock(side_effect=_capture_send)
    await bot.send_eod_summary()

    assert sent_messages, "EOD summary message was not sent"
    payload = sent_messages[-1]
    assert "[DB UNAVAILABLE - SHOWING SESSION DATA ONLY]" in payload
    assert "NSE:FALLBACK-EQ" in payload
