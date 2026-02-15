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
CAPITAL_PER_TRADE = 1800  # INR per trade (Safety buffer for 2k account)
CAPITAL = CAPITAL_PER_TRADE  # Backward-compatible alias
RISK_PER_TRADE = 200 # Max loss in INR (optional logic)
AUTO_TRADE = False # Default off

# System Config
LOG_FILE = "logs/bot.log"
SQUARE_OFF_TIME = "15:10" # HH:MM (24-hour format)
VALIDATION_TIMEOUT_MINUTES = 15  # Phase 41.1: Reduced from 45

# --- Phase 41.1: Multi-Edge Detection System ---
MULTI_EDGE_ENABLED = False  # Master switch (set True to activate)
CONFIDENCE_THRESHOLD = "MEDIUM"  # Minimum confidence to proceed

# Individual Detector Toggles (Phase 41.1: 5 active, 3 removed)
ENABLED_DETECTORS = {
    "PATTERN": True,            # P0 — Existing 6-pattern engine (always on)
    "TRAPPED_POSITION": True,   # P0 — Trapped longs via volume + depth
    "ABSORPTION": True,         # P0 — Hidden limit sellers at highs
    "BAD_HIGH": True,           # P0 — DOM sell-wall + rejection wick
    "FAILED_AUCTION": True,     # P1 — Simplified in 41.1 (30-candle min)
    # REMOVED in Phase 41.1:
    # "OI_DIVERGENCE_PROXY": False,   # Was P3 — noise without real OI
    # "TPO_POOR_HIGH": False,         # Was P3 — 50-100ms overhead, 95% info loss
    # "MOMENTUM_EXHAUSTION": False,   # Was P0 — redundant with Gate 7
}

LOG_MULTI_EDGE_DETAILS = True  # Log all edge detection attempts

# --- Phase 41.1: Weighted Confluence Scoring ---
EDGE_WEIGHTS = {
    "ABSORPTION": 3.0,                  # Rare, extreme conviction
    "BAD_HIGH": 2.0,                    # Orderflow + price rejection = strong
    "TRAPPED_LONGS": 2.0,               # High conviction institutional trap
    "PATTERN_SHOOTING_STAR": 1.5,       # Strong pattern
    "PATTERN_MOMENTUM_BREAKDOWN": 1.5,  # Strong pattern
    "PATTERN_BEARISH_ENGULFING": 1.0,   # Standard pattern
    "PATTERN_EVENING_STAR": 1.0,        # Standard pattern
    "PATTERN_VOLUME_TRAP": 1.0,         # Standard pattern
    "PATTERN_ABSORPTION_DOJI": 2.0,     # Actually absorption-class
    "FAILED_AUCTION": 1.0,              # Common setup
}

# Weighted confidence thresholds
CONFIDENCE_THRESHOLD_EXTREME = 5.0
CONFIDENCE_THRESHOLD_HIGH = 3.0
CONFIDENCE_THRESHOLD_MEDIUM = 2.0

# --- Phase 41.1: Performance Tracking ---
ENABLE_DETECTOR_TRACKING = True
DETECTOR_LOG_PATH = "logs/detector_performance.csv"

# --- Phase 41.1: Scanner Optimization ---
SCANNER_PARALLEL_WORKERS = 10  # Max concurrent API calls

# ============================================================================
# SCALPER RISK MANAGEMENT (Phase 41.2)
# ============================================================================

# Feature flag — Set to True to enable new scalper system
USE_SCALPER_RISK_MANAGEMENT = False  # DEFAULT: OFF (backward compatible)

# Stop Loss Configuration
SCALPER_STOP_TICK_BUFFER = 12           # Ticks above setup candle high
SCALPER_STOP_HUNT_BUFFER_PCT = 0.003    # 0.3% hunt protection buffer
SCALPER_STOP_HUNT_BUFFER_ENABLED = True

# Breakeven Configuration
SCALPER_BREAKEVEN_TRIGGER_PCT = 0.003   # 0.3% profit triggers breakeven

# Trailing Stop Configuration
SCALPER_TRAILING_DISTANCE_INITIAL = 0.002     # 0.2% behind price
SCALPER_TRAILING_DISTANCE_AFTER_TP1 = 0.0015  # 0.15% (tighter after TP1)
SCALPER_TRAILING_DISTANCE_AFTER_TP2 = 0.001   # 0.1% (very tight after TP2)

# Take Profit Targets
SCALPER_TP1_PCT = 0.015  # 1.5% — Close 50%
SCALPER_TP2_PCT = 0.025  # 2.5% — Close another 25% (75% total)
SCALPER_TP3_PCT = 0.035  # 3.5% — Close all remaining

# Simulation & Analysis
ENABLE_EOD_SIMULATION = True
SIMULATION_LOG_PATH = "logs/eod_simulation.csv"

# ============================================================================
# POSITION SAFETY (Phase 42 — CRITICAL)
# ============================================================================

# Enable position verification before every order
ENABLE_POSITION_VERIFICATION = True    # NEVER set to False in production

# Enable broker position checks in focus loop
ENABLE_BROKER_POSITION_POLLING = True

# Position reconciliation frequency (seconds)
POSITION_RECONCILIATION_INTERVAL = 1800  # 30 minutes

# Emergency alerts
EMERGENCY_ALERT_ENABLED = True
EMERGENCY_LOG_PATH = 'logs/emergency_alerts.log'
ORPHANED_POSITION_LOG_PATH = 'logs/orphaned_positions.log'

# ============================================================================
# CAPITAL MANAGEMENT (Phase 42.1)
# ============================================================================

# Fixed 5× intraday leverage (NSE standard)
# ₹1,800 × 5 = ₹9,000 buying power
# DO NOT CHANGE (user requirement: always use 5× leverage)
INTRADAY_LEVERAGE = 5.0

# Signal logging
SIGNAL_LOG_PATH = 'logs/signals.csv'
LOG_ALL_SIGNALS = True  # Log executed AND skipped signals


