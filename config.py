import os
import json
import datetime
import pytz
from dotenv import load_dotenv
from pathlib import Path

# Load environment variables
load_dotenv()

# ============================================================================
# 1. CREDENTIALS & SENSITIVE DATA
# ============================================================================
FYERS_CLIENT_ID = os.getenv("FYERS_CLIENT_ID")
FYERS_SECRET_ID = os.getenv("FYERS_SECRET_ID")
FYERS_REDIRECT_URI = os.getenv("FYERS_REDIRECT_URI", "https://trade.fyers.in/api-login/redirect-uri/index.html")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# ============================================================================
# 2. CORE TRADING CONFIG (CRITICAL)
# ============================================================================
# Session Safety
AUTO_MODE = True            # Controls if the bot auto-executes trades (can be toggled via Telegram)
MAX_SESSION_LOSS_INR = 500  # Max cumulative intra-day loss before bot halts (Phase 69)
DAILY_TARGET_INR = -1       # Set to -1 for Dynamic 5% Mode (Automatic calculation)
                            # Or set a fixed amount like ₹75 to override.
                            # When hit: only EXTREME or MAX_CONVICTION signals allowed.
INTRADAY_LEVERAGE = 5.0    # Fixed 5× leverage (NSE standard requirement)

# Phase 94: Trade Direction Switch
# Controls whether bot enters SHORT (SELL) or LONG (BUY) positions.
# Default: SHORT. Toggle via Telegram /mode buy | /mode sell at runtime.
TRADE_DIRECTION = 'SHORT'  # 'SHORT' or 'LONG'

# Timing (IST)
# 0 disables the time-based exit. Positions run until the stop-loss or the EOD
# square-off — the same reasoning that removed the take-profit: a 45-minute cap
# closed winners early, and the only profitable trade in the first two live days
# needed 67 minutes to develop. Set a positive value to re-enable.
MAX_HOLD_TIME_MINUTES = 0

# ============================================================================
# 3. SCANNER & G5 STRETCH CONSTANTS
# ============================================================================
# Gain Floors & Limits
SCANNER_GAIN_MIN_PCT: float = 7.5  # Phase 65: Synchronized with P65_G1 floor
SCANNER_GAIN_MAX_PCT: float = 18.0 # Protection against upper-circuit runners
SCANNER_MIN_VOLUME:   int   = 333333 # Phase 91.3: Adjusted to 333K as requested
SCANNER_MIN_LTP:      float = 40.0   # Filter sub-₹40 manipulation vehicles
CANDLE_BODY_RATIO_MIN: float = 0.382   # Phase 91.3: Scientific threshold (Fibonacci 0.382) for "clean" bodies

# G5 Stretch Thresholds
DAY_GAIN_PCT_THRESHOLD = 7.5       # Duplicate alias used in legacy paths

# Operations
SCANNER_PARALLEL_WORKERS = 3 # Reverted to 3 to prevent Fyers 429 Rate Limits
WS_TICK_FRESHNESS_TTL_SECONDS = 180.0

# ============================================================================
# STRATEGY: BackToVWAPShort
# ============================================================================
STRATEGY_VWAP_SD_FLOOR: float = 3.3       # Lowered from 4.5 — allows moderately stretched setups
STRATEGY_VWAP_SD_HIGH: float = 5.0        # HIGH confidence tier threshold
STRATEGY_VWAP_SD_EXTREME: float = 6.0     # EXTREME confidence tier threshold
STRATEGY_REQUIRE_FAILED_AUCTION: bool = True  # Hard gate: require auction failure behavior
STRATEGY_VOL_FADE_MAX_RATIO: float = 0.65    # Volume fade ratio (< this = fading) — absolute, no relaxation
STRATEGY_VOL_FADE_LOOKBACK: int = 3          # Candles to look back for volume baseline (shortened from 15)
STRATEGY_RSI_DIVERGENCE_WINDOW: int = 10      # Window for swing-based RSI divergence check (shortened from 25)
STRATEGY_MOMENTUM_DECAY_RATIO: float = 0.85  # Fast slope must be < slow * this ratio

# ============================================================================
# 6. EXIT ENGINE & RISK MULTIPLIERS
# ============================================================================
SL_ATR_MULTIPLIER = 0.5
SL_MIN_TICK_BUFFER = 3

P52_CLEANUP_ON_STOP_FOCUS: bool = True 

# ============================================================================
# 7. LOGGING (PHASE 70-74)
# ============================================================================
LOG_FILE = "logs/bot.log"

# ============================================================================
# 8. FEATURE TOGGLES & LEGACY (PHASE 41 - PHASE 44)
# ============================================================================

RVOL_VALIDITY_GATE_ENABLED = True
RVOL_MIN_CANDLES = 15

# Phase 44.4: Telegram UX
ETF_CLUSTER_DEDUP_ENABLED = True
ETF_CLUSTER_KEYWORDS = ["SILVER"]

# Legacy & Backward Compatibility
TRADING_ENABLED = False 

MARKET_SESSION_CONFIG = {
    'allow_postmarket_sleep': True,
    'telegram_state_transitions': True
}

def set_trading_enabled(val: bool):
    global TRADING_ENABLED
    TRADING_ENABLED = val

def minutes_since_market_open() -> float:
    """Calculate minutes elapsed since 09:15 IST today."""
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.datetime.now(tz)
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    if now < market_open:
        return 0.0
    delta = now - market_open
    return delta.total_seconds() / 60.0

# Phase 81: Telegram Hardening & Menu
P81_TELEGRAM_MENU_ENABLED        = True
P81_TELEGRAM_RATE_LIMIT_HZ       = 2

# ============================================================================
# PHASE 82: LOCAL CANDLE ENGINE
# ============================================================================
P82_LOCAL_CANDLES_ENABLED = True
P82_MAX_LOCAL_CANDLES = 500

# ============================================================================
# RESTORED MISSING PHASE CONSTANTS (Fixes runtime crashes)
# ============================================================================

MARKET_REGIME_CONFIG = {
    'strong_trend_threshold': 0.015
}
ENABLE_MARKET_REGIME_FILTER = False  # Set to False to disable the Nifty 50 trend block

P61_G9_BYPASS_SD_THRESHOLD = 5.0
P61_G9_ACCEL_REJECT_THRESHOLD = 0.5
P61_G9_STALL_PASS_THRESHOLD = 0.1

P58_G12_USE_CANDLE_CLOSE = False
P65_AMT_ENABLED = True
