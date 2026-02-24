import pytest

from telegram_bot import ShortCircuitBot


class DummyApp:
    def __init__(self):
        self.updater = None
        self.running = False
        self.shutdown_called = False

    async def stop(self):
        self.running = False

    async def shutdown(self):
        self.shutdown_called = True


@pytest.mark.asyncio
async def test_stop_handles_none_app():
    bot = ShortCircuitBot(
        {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "1"},
        None,
        None,
        None,
    )
    bot.app = None
    await bot.stop()


@pytest.mark.asyncio
async def test_stop_handles_missing_updater():
    bot = ShortCircuitBot(
        {"TELEGRAM_BOT_TOKEN": "test-token", "TELEGRAM_CHAT_ID": "1"},
        None,
        None,
        None,
    )
    dummy = DummyApp()
    bot.app = dummy
    await bot.stop()
    assert dummy.shutdown_called is True
