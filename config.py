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

# Timing (IST)
SQUARE_OFF_TIME = "15:10" 
VALIDATION_TIMEOUT_MINUTES = 15 

# ============================================================================
# 3. SCANNER & G5 STRETCH CONSTANTS
# ============================================================================
# Gain Floors & Limits
SCANNER_GAIN_MIN_PCT: float = 7.5  # Phase 65: Synchronized with P65_G1 floor
SCANNER_GAIN_MAX_PCT: float = 18.0 # Protection against upper-circuit runners
SCANNER_MIN_VOLUME:   int   = 100000 # Minimum liquidity floor
SCANNER_MIN_LTP:      float = 40.0   # Filter sub-₹40 manipulation vehicles

# G5 Stretch Thresholds
G5_STRETCH_LOW_PCT:  float = 7.5   # Synchronized with Scanner Min
G5_STRETCH_HIGH_PCT: float = 14.5
DAY_GAIN_PCT_THRESHOLD = 7.5       # Duplicate alias used in legacy paths

# Operations
SCANNER_PARALLEL_WORKERS = 3
WS_TICK_FRESHNESS_TTL_SECONDS = 180.0

# ============================================================================
# 4. GATES HARDENING (PHASE 51 - PHASE 65)
# ============================================================================
PHASE_51_ENABLED = True

# G1: Constraints & Time-Since-High
P51_G1_KILL_BACKDOOR = True
P51_G1_KILL_BACKDOOR_FIXED_PCT: float = 0.015 
P51_G1_KILL_BACKDOOR_ATR_MULT:  float = 0.3   
P51_G1_KILL_BACKDOOR_USE_ATR:   bool  = True  

# G4: Momentum & Slope
P51_G4_RVOL_THRESHOLD = 5.0
P51_G4_SLOPE_MIN = 3.0  # Unit: bp/min (recalibrated 2026-03-10)

# G5: Exhaustion & Lookbacks
P51_G5_GATE_B_USE_ALLDAY_HIGH = True
P51_G5_GATE_B_FIXED_TOLERANCE = 0.005
P51_G5_GATE_B_ATR_MULT = 0.2        
P55_G5_GATE_B_DAY_HIGH_TOLERANCE = 0.003
P55_G5_VOL_FADE_LOOKBACK = 15 
P51_G5_ATR_EXTREME_STRETCH_MULT = 3.5
P51_G5_GATE_E_LATE_SESSION_EXTREME_ONLY = False

# G6: RSI & Confluence
P55_G6_RSI_DIVERGENCE_WINDOW: int = 25
EDGE_WEIGHTS = {
    "ABSORPTION": 3.0, "BAD_HIGH": 2.0, "TRAPPED_LONGS": 2.0,
    "PATTERN_SHOOTING_STAR": 1.5, "PATTERN_MOMENTUM_BREAKDOWN": 1.5,
    "PATTERN_BEARISH_ENGULFING": 1.0, "PATTERN_EVENING_STAR": 1.0,
    "PATTERN_VOLUME_TRAP": 1.0, "FAILED_AUCTION": 1.0,
}
CONFIDENCE_THRESHOLD_EXTREME = 5.0
CONFIDENCE_THRESHOLD_HIGH    = 3.0
CONFIDENCE_THRESHOLD_MEDIUM  = 2.0

# G7: Regime & Timing
P51_G7_TIME_GATE_ENABLED = True   # Enables EOD cutoff logic
MARKET_REGIME_CONFIG = {
    'strong_trend_threshold': 0.015,
    'extreme_regime_buffer': 0.005
}

# G8: Risk Management (Updated to Unlimited)
P51_G8_SIGNAL_COOLDOWN_MINUTES = 45 
P51_G8_COOLDOWN_ON_SIGNAL_ADD = True 

# G9 & G10: HTF & Spread
P61_G9_MATH_LOGIC_ENABLED = True
P61_G9_BYPASS_SD_THRESHOLD = 3.0
P61_G9_ACCEL_REJECT_THRESHOLD = 2.0
P61_G9_STALL_PASS_THRESHOLD = 1.0
P51_G10_MAX_SPREAD_PCT = 0.004

# G11-G12: Validation
P51_G11_MIN_REMAINING_MINUTES = 15
P51_G12_INVALIDATION_BUFFER_PCT = 0.002
P58_G12_USE_CANDLE_CLOSE = True

# ============================================================================
# 5. MEAN REVERSION PHYSICS (PHASE 57 - PHASE 66)
# ============================================================================
P57_G4_SLOPE_DECAY_ENABLED = True
P57_G4_DIVERGENCE_SD = 1.5
P57_G5_Z_EXTREME_THRESHOLD = 3.3
P57_G5_Z_FADE_RELAXATION = 0.95
P60_G4_STRUCTURAL_FALLBACK_GAIN = 10.0
P60_G5_SPEAR_VOL_CLIMAX_MULT = 3.0
P66_G4_DECAY_SD_THRESHOLD = 2.0

# Phase 65: AMT & Opening Climax
P65_AMT_ENABLED = True
P65_G7_CLIMAX_WINDOW_START = "09:30"
P65_G7_SAFE_TRADE_START = "10:00"
P65_G7_CLIMAX_SD_THRESHOLD = 3.0
P65_G7_VOLUME_Z_SCORE_THRESHOLD = 2.0

P65_G1_NET_GAIN_THRESHOLD = 7.5
P65_G1_NORMAL_THRESHOLD = 9.0    
P65_G1_AMT_REQUIRED_BELOW_9 = True
P65_G1_TIME_SINCE_HIGH_CANDLES = 45

# Phase 86: Adaptive Momentum Decay — No longer uses 120s timer
# Murphy: Momentum divergence IS the confirmation.
P66_ADAPTIVE_G1_ENABLED: bool = True
P66_G1_ROTATION_THRESHOLD_PCT: float = 0.030 
# P66_G4_DECAY_CONFIRMATION_SEC: int = 120 (Deprecated in Phase 86)

# ============================================================================
# PHASE 79: LEVERAGE GUARD (G14)
# ============================================================================
P79_G14_LEVERAGE_GUARD_ENABLED = True
P79_G14_MIN_LEVERAGE = 1.1  # Reject if leverage < 1.1 (Allows all non-1x stocks)

# ============================================================================
# 6. EXIT ENGINE & RISK MULTIPLIERS
# ============================================================================
P51_SL_ATR_MULTIPLIER = 0.5
P51_SL_MIN_TICK_BUFFER = 3

# Phase 78: Single TP Multipliers (No Partials)
P78_SINGLE_TP_ATR_MULT_DEFAULT = 1.0
P78_SINGLE_TP_ATR_MULT_LOW_GAIN = 0.5
P52_BREAKEVEN_AFTER_TP1: bool = True   
P52_SL_MOVE_AFTER_TP2: bool = True     
P52_CLEANUP_ON_STOP_FOCUS: bool = True 
P52_HARD_STOP_RECONCILE_SECONDS: int = 30 

# Discretionary Exit Brain (Phase 41.3)
DISCRETIONARY_CONFIG = {
    'soft_stop_pct': 0.005,
    'bullish_exit_threshold': 3,
    'bearish_hold_threshold': 3,
    'min_time_before_exit_minutes': 15,
    'momentum_extend_threshold': 5
}

# ============================================================================
# 7. LOGGING, HUD, & ML OVERRIDES (PHASE 70-74)
# ============================================================================
# Logging Paths
LOG_FILE = "logs/bot.log"
SIGNAL_LOG_PATH = "logs/signals.csv"
DETECTOR_LOG_PATH = "logs/detector_performance.csv"
EMERGENCY_LOG_PATH = "logs/emergency_alerts.log"
ORPHANED_POSITION_LOG_PATH = "logs/orphaned_positions.log"

# AEGIS HUD (Phase 72)
P72_DASHBOARD_ENABLED = True
P72_DASHBOARD_PORT = 8555

# ML Weekend Overrides (Phase 70)
P70_ML_DYNAMIC_OVERRIDE_ENABLED = False

if P70_ML_DYNAMIC_OVERRIDE_ENABLED:
    DYNAMIC_CONFIG_PATH = Path("data/ml/dynamic_config.json")
    if DYNAMIC_CONFIG_PATH.exists():
        try:
            with open(DYNAMIC_CONFIG_PATH, 'r') as f:
                dynamic_overrides = json.load(f)
            import sys
            _mod = sys.modules[__name__]
            for key, val in dynamic_overrides.items():
                if hasattr(_mod, key):
                    setattr(_mod, key, val)
        except Exception as e:
            print(f"❌ Failed to load dynamic_config.json: {e}")

# ============================================================================
# 8. FEATURE TOGGLES & LEGACY (PHASE 41 - PHASE 44)
# ============================================================================
MULTI_EDGE_ENABLED = False
RVOL_VALIDITY_GATE_ENABLED = True
ENABLE_POSITION_VERIFICATION = True
ENABLE_BROKER_POSITION_POLLING = True
ENABLE_DIAGNOSTIC_ANALYZER = True
LOG_MULTI_EDGE_DETAILS = True
POSITION_RECONCILIATION_INTERVAL = 1800
EMERGENCY_ALERT_ENABLED = True
RVOL_MIN_CANDLES = 15

# Phase 44.4: Telegram UX
ETF_CLUSTER_DEDUP_ENABLED = True
ETF_CLUSTER_KEYWORDS = ["SILVER"]
EDITABLE_SIGNAL_FLOW_ENABLED = False

# Multi-Edge Detectors (Phase 41.1)
ENABLED_DETECTORS = {
    "PATTERN": True,            # P0 — Existing 6-pattern engine
    "TRAPPED_POSITION": True,   # P0 — Trapped longs
    "ABSORPTION": True,         # P0 — Hidden limit sellers
    "BAD_HIGH": True,           # P0 — DOM sell-wall
    "FAILED_AUCTION": True,     # P1 — Simplified in 41.1
}

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
