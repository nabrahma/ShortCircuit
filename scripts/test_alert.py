import logging
import config
from notifier import TelegramNotifier

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def test_alert():
    logger.info("Testing Telegram Alert...")
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        logger.error("Missing Telegram keys in .env! Cannot test.")
        return

    notifier = TelegramNotifier()
    notifier.send_alert("TEST-STOCK", 100.0, 101.5, "SHORT (TEST)")

if __name__ == "__main__":
    test_alert()
