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
AUTO_MODE = False          # NEVER change this to True. Enable via Telegram /auto only.
AUTO_MODE_DEFAULT = False 
MAX_SESSION_LOSS_INR = 500  # Max cumulative intra-day loss before bot halts (Phase 69)
DAILY_TARGET_INR = -1       # Set to -1 for Dynamic 5% Mode (Automatic calculation)
                            # Or set a fixed amount like ₹75 to override.
                            # When hit: only EXTREME or MAX_CONVICTION signals allowed.
INTRADAY_LEVERAGE = 5.0    # Fixed 5× leverage (NSE standard requirement)
MIN_LEVERAGE = 3.0         # Minimum leverage allowed for a stock to pass the scanner
CAPITAL_PER_TRADE = 9000   # OFFLINE FALLBACK ONLY (Buying power for 1800 margin)

# Phase 94: Trade Direction Switch
# Controls whether bot enters SHORT (SELL) or LONG (BUY) positions.
# Default: SHORT. Toggle via Telegram /mode buy | /mode sell at runtime.
TRADE_DIRECTION = 'SHORT'  # 'SHORT' or 'LONG'

# Timing (IST)
SQUARE_OFF_TIME = "15:10" 
VALIDATION_TIMEOUT_MINUTES = 15 
MAX_HOLD_TIME_MINUTES = 45

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
G5_STRETCH_LOW_PCT:  float = 7.5   # Synchronized with Scanner Min
G5_STRETCH_HIGH_PCT: float = 14.5
DAY_GAIN_PCT_THRESHOLD = 7.5       # Duplicate alias used in legacy paths

# Operations
SCANNER_PARALLEL_WORKERS = 10 # Phase 91.3: Increased from 3 to 10 for faster history fetching
WS_TICK_FRESHNESS_TTL_SECONDS = 180.0

# ============================================================================
# STRATEGY: BackToVWAPShort
# ============================================================================
STRATEGY_VWAP_SD_FLOOR: float = 3.3       # Lowered from 4.5 — allows moderately stretched setups
STRATEGY_VWAP_SD_HIGH: float = 5.0        # HIGH confidence tier threshold
STRATEGY_VWAP_SD_EXTREME: float = 6.0     # EXTREME confidence tier threshold
STRATEGY_REQUIRE_FAILED_AUCTION: bool = True  # Hard gate: require auction failure behavior
STRATEGY_VOL_FADE_MAX_RATIO: float = 0.65    # Volume fade ratio (< this = fading) — absolute, no relaxation
STRATEGY_VOL_FADE_LOOKBACK: int = 15         # Candles to look back for volume baseline
STRATEGY_RSI_DIVERGENCE_WINDOW: int = 25      # Window for swing-based RSI divergence check
STRATEGY_MOMENTUM_DECAY_RATIO: float = 0.85  # Fast slope must be < slow * this ratio


# ============================================================================
# PHASE 79: LEVERAGE GUARD (G14)
# ============================================================================
P79_G14_LEVERAGE_GUARD_ENABLED = True
P79_G14_MIN_LEVERAGE = 1.1  # Reject if leverage < 1.1 (Allows all non-1x stocks)

# ============================================================================
# 6. EXIT ENGINE & RISK MULTIPLIERS
# ============================================================================
SL_ATR_MULTIPLIER = 0.5
SL_MIN_TICK_BUFFER = 3

# Phase 78: Single TP Multipliers (No Partials)
P78_SINGLE_TP_ATR_MULT_DEFAULT = 1.0
P78_SINGLE_TP_ATR_MULT_LOW_GAIN = 0.5
P52_BREAKEVEN_AFTER_TP1: bool = True   
P52_SL_MOVE_AFTER_TP2: bool = True     
P52_CLEANUP_ON_STOP_FOCUS: bool = True 
P52_HARD_STOP_RECONCILE_SECONDS: int = 30 



# ============================================================================
# 7. LOGGING & ML OVERRIDES (PHASE 70-74)
# ============================================================================
# Logging Paths
LOG_FILE = "logs/bot.log"
SIGNAL_LOG_PATH = "logs/signals.csv"
DETECTOR_LOG_PATH = "logs/detector_performance.csv"
EMERGENCY_LOG_PATH = "logs/emergency_alerts.log"
ORPHANED_POSITION_LOG_PATH = "logs/orphaned_positions.log"

# ML Dynamic Override: DISABLED per PRD.
# Research variants must never modify live config at runtime.
P70_ML_DYNAMIC_OVERRIDE_ENABLED = False

# ============================================================================
# 8. FEATURE TOGGLES & LEGACY (PHASE 41 - PHASE 44)
# ============================================================================

RVOL_VALIDITY_GATE_ENABLED = True
ENABLE_POSITION_VERIFICATION = True
ENABLE_BROKER_POSITION_POLLING = True
POSITION_RECONCILIATION_INTERVAL = 1800
EMERGENCY_ALERT_ENABLED = True
RVOL_MIN_CANDLES = 15

# Phase 44.4: Telegram UX
ETF_CLUSTER_DEDUP_ENABLED = True
ETF_CLUSTER_KEYWORDS = ["SILVER"]




# Legacy & Backward Compatibility
CAPITAL = 1800              # OFFLINE FALLBACK ONLY (Real Margin)
CONFIDENCE_THRESHOLD = "MEDIUM" 
TRADING_ENABLED = False 
RECOVERY_FILE_PATH = "data/recovery.json"

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
P81_TELEGRAM_BUFFER_WINDOW_SEC   = 2.0
P81_TELEGRAM_STOP_CONFIRM_TIMEOUT = 30

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

P61_G9_BYPASS_SD_THRESHOLD = 5.0
P61_G9_ACCEL_REJECT_THRESHOLD = 0.5
P61_G9_STALL_PASS_THRESHOLD = 0.1

P58_G12_USE_CANDLE_CLOSE = False
P65_AMT_ENABLED = True
