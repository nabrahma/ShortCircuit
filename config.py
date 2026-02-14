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

# --- Phase 41: Multi-Edge Detection System ---
MULTI_EDGE_ENABLED = False  # Master switch (set True to activate)
CONFIDENCE_THRESHOLD = "MEDIUM"  # Minimum confidence to proceed

# Individual Detector Toggles
ENABLED_DETECTORS = {
    "PATTERN": True,            # P0 — Existing 6-pattern engine (always on)
    "TRAPPED_POSITION": False,  # P1 — Trapped longs via volume + depth
    "ABSORPTION": False,        # P1 — Hidden limit sellers at highs
    "BAD_HIGH": False,          # P1 — DOM sell-wall + rejection wick
    "FAILED_AUCTION": False,    # P2 — Failed breakout via balance area
    "OI_DIVERGENCE_PROXY": False,  # P3 — Volume-momentum divergence
    "TPO_POOR_HIGH": False,     # P3 — Thin acceptance zones
    "MOMENTUM_EXHAUSTION": False,  # P0 — VWAP extension + green run
}

LOG_MULTI_EDGE_DETAILS = True  # Log all edge detection attempts

