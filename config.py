import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Fyers Credentials
FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
FYERS_SECRET_ID = os.getenv("FYERS_SECRET_ID")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")

# Telegram Credentials
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Trading Config
CAPITAL = 1800  # INR per trade (Safety buffer for 2k account)
RISK_PER_TRADE = 200 # Max loss in INR (optional logic)
AUTO_TRADE = False # Default off

# System Config
LOG_FILE = "logs/bot.log"
SQUARE_OFF_TIME = "15:10" # HH:MM (24-hour format)
