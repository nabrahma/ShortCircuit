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
# DEPRECATED Phase 44.6 — real margin now fetched from Fyers /funds
CAPITAL_PER_TRADE = 1800  # INR per trade (Safety buffer for 2k account)
CAPITAL = CAPITAL_PER_TRADE  # Backward-compatible alias
MAX_SESSION_LOSS_INR = 500  # Max cumulative intra-day loss before bot halts
# ============================================================
# AUTO TRADE GATE
# CRITICAL: Must ALWAYS be False on startup.
# Only Telegram /auto on command can enable live trading.
# ============================================================
AUTO_MODE = False          # NEVER change this to True
AUTO_MODE_DEFAULT = False  # Backup safety — used as fallback

# Read from env but OVERRIDE to False if somehow set True in .env
_env_auto = os.getenv('AUTO_MODE', 'false').lower()
AUTO_MODE = False  # Ignore env — always False on boot

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
SCANNER_PARALLEL_WORKERS = 3  # Reduced from 10 — Fyers rate-limits parallel candle fetches
WS_TICK_FRESHNESS_TTL_SECONDS = 180.0  # SymbolUpdate fires on price change only; flat stocks idle 2-3 min

# ============================================================================
# SCALPER RISK MANAGEMENT (Phase 41.2)
# ============================================================================

import datetime
import pytz

# Early Session Data Validity Gate Phase 44.3
RVOL_MIN_CANDLES = 15           # Reduced to 15 for Phase 65 Climax Window (09:30+)
RVOL_VALIDITY_GATE_ENABLED = True  # Feature flag — set False to disable instantly

def minutes_since_market_open() -> float:
    """Returns minutes elapsed since NSE market open (9:15 AM IST) safely accounting for server timezone."""
    IST = pytz.timezone('Asia/Kolkata')
    now_ist = datetime.datetime.now(IST)
    open_today = now_ist.replace(hour=9, minute=15, second=0, microsecond=0)
    return max(0.0, (now_ist - open_today).total_seconds() / 60.0)

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

# ============================================================================
# DIAGNOSTIC ANALYZER (Phase 42.2)
# ============================================================================

# Enable/disable the /why command and diagnostic analysis
ENABLE_DIAGNOSTIC_ANALYZER = True

# CSV log of all diagnostic runs (tracks which gates fail most often)
DIAGNOSTIC_LOG_PATH = 'logs/diagnostic_analysis.csv'

# ── PHASE 41.3: INTELLIGENT EXITS ──────────────────────────
ENABLE_DISCRETIONARY_EXITS = True
ENABLE_HARD_STOPS = True
ENABLE_MARKET_REGIME_OPTIMIZATION = True

# Market Regime Configuration
MARKET_REGIME_CONFIG = {
    'strong_trend_threshold': 0.015,  # 1.5%
    'override_patterns': ['EVENING_STAR', 'BEARISH_ENGULFING', 'SHOOTING_STAR'],
    'divergence_threshold': -2.0,  # Stock down 2%
}

# Discretionary Exit Configuration
DISCRETIONARY_CONFIG = {
    'soft_stop_pct': 0.005,  # 0.5%
    'hard_stop_pct': 0.02,  # 2.0%
    'initial_target_pct': 0.025,  # 2.5%
    'extended_target_pct': 0.04,  # 4.0%
    'bearish_exit_threshold': 2,    # >= 2 more Bearish signals -> Force Exit
    'momentum_extend_threshold': 2  # Score >= 2 -> Extend Target
}

# ── PHASE 41.3.1: MARKET SESSION CONFIG ──────────────────────
MARKET_SESSION_CONFIG = {
    # Time thresholds (IST)
    'market_open': '09:15',
    'safe_trade_start': '09:30',
    'eod_cutoff': '15:10',
    'market_close': '15:30',
    
    # Startup behavior
    'allow_postmarket_sleep': True,   # Sleep overnight vs exit
    'enable_warmup_scanning': True,   # Scan during 9:15-9:30
    'require_morning_data': True,     # Fetch historical data
    
    # Fallback
    'morning_range_fallback_pct': 0.5,# ±0.5% if API fails
    
    # Notifications
    'telegram_startup_alert': True,
    'telegram_state_transitions': True,
}

# Trading enable/disable flag (dynamically updated by MarketSession)
TRADING_ENABLED = False  # Default to False

def set_trading_enabled(value):
    """Update trading enabled flag"""
    global TRADING_ENABLED
    TRADING_ENABLED = value
    # Using print as logger might not be configured in config context, 
    # but main.py will configure it.
    # We can't import logger here to avoid circular deps if logger uses config.
    pass

# Phase 41.3.2: EOD Analysis
EOD_CONFIG = {
    'audit_slippage_threshold': 0.005, # >0.5% slippage is anomalous
    'report_format': 'Markdown',
    'save_reports_locally': True,
    'auto_send_telegram': True
}

# ============================================================================
# PHASE 44.4: TELEGRAM UX + EXECUTION OBSERVABILITY
# ============================================================================

# ETF Cluster Deduplication (Section 7)
ETF_CLUSTER_DEDUP_ENABLED = True
ETF_CLUSTER_KEYWORDS = ["SILVER"]  # Extend: ["SILVER", "GOLD", "NIFTY"]

# Editable Signal Message Flow (Section 2.1/2.2) — behind feature flag
EDITABLE_SIGNAL_FLOW_ENABLED = False  # Set True only after stability confirmed

# ============================================================================
# PHASE 44.8: GATE 5 STRETCH CONSTANTS
# ============================================================================

# Minimum intraday gain % for a stock to pass the scanner filter (gain >= this to be a candidate)
# Also used as the stretch_score baseline in G5: score = (gain_pct - BASE) / BASE
# Source: scanner.py gain filter threshold
SCANNER_GAIN_MIN_PCT: float = 9.0  # Phase 51: Up from 6.18

# G5 stretch sweet spot window (gain_pct must be in [STRETCH_LOW, STRETCH_HIGH])
G5_STRETCH_LOW_PCT:  float = 9.0
G5_STRETCH_HIGH_PCT: float = 14.5

# Minimum LTP for a stock to be considered a scanner candidate.
# Stocks below this threshold are disproportionately operator-driven on NSE
# and produce unreliable RVOL, VAH, and tape signals.
# ₹50 filters sub-₹50 manipulation vehicles while keeping all legitimate
# small/mid-cap candidates that your strategy targets.
SCANNER_MIN_LTP: float = 50.0

# ============================================================================
# PHASE 51: GATE QUALITY HARDENING (Section 1)
# ============================================================================
PHASE_51_ENABLED = True

# G1: Constraints & Time-Since-High
P51_G1_TIME_SINCE_HIGH_CANDLES = 20
P51_G1_KILL_BACKDOOR = True # Reject if ltp < day_high - threshold (Section 2.2)

# G1: Kill Backdoor — ATR-relative threshold
# If P51_G1_KILL_BACKDOOR_USE_ATR is True, threshold = max(FIXED_PCT, ATR_MULT × ATR%).
# If False (or ATR unavailable), falls back to FIXED_PCT alone.
P51_G1_KILL_BACKDOOR_FIXED_PCT: float = 0.015  # 1.5% fixed floor (increased from 1.0%)
P51_G1_KILL_BACKDOOR_ATR_MULT:  float = 0.3    # 0.3 × ATR as % of day_high
P51_G1_KILL_BACKDOOR_USE_ATR:   bool  = True   # True = ATR-relative mode (recommended)

# G2: Data Quality

# G3: Circuit Hitter Blacklist
P51_G3_CIRCUIT_TOUCH_TIMEOUT_MINUTES = 60

# G4: Sustained Momentum
P51_G4_RVOL_THRESHOLD = 5.0
# Unit: basis points of VWAP per 1-min candle over 30-candle regression window.
# 3.0 bp/min = stock rising ~0.9% in last 30 min → acceptable to short.
# Above 3.0 = stock actively surging (freight train) → DO NOT SHORT.
# Reference: own codebase labels <5 bp/min as "FLAT" in god_mode_logic.py:48.
# Literature: Brooks/Minervini/Weinstein consensus places "too strong" at ≥3.3 bp/min.
P51_G4_SLOPE_MIN = 3.0  # was 0.5 — recalibrated 2026-03-10

# G5: Exhaustion Stretch
P51_G5_GATE_B_USE_ALLDAY_HIGH = True
P51_G5_GATE_B_FIXED_TOLERANCE = 0.005 # 0.5% floor for noise
P51_G5_GATE_B_ATR_MULT = 0.2         # 0.2 * ATR scaling
P51_G5_GATE_D_ATR_CLEARANCE = True
P51_G5_GATE_E_LATE_SESSION_EXTREME_ONLY = True
P51_G5_ATR_EXTREME_STRETCH_MULT = 3.5

# ============================================================================
# PHASE 57: MEAN REVERSION OPTIMIZATION (Guo-Zhang Model)
# ============================================================================
P57_G4_SLOPE_DECAY_ENABLED: bool = True
P57_G4_DIVERGENCE_SD: float = 1.5      # Murphy: Momentum divergence needs price extension

P57_G5_Z_EXTREME_THRESHOLD: float = 3.3 # Guo-Zhang: Absorption threshold
P57_G5_Z_FADE_RELAXATION: float = 0.95   # Allow higher vol if price stalled at extreme

# G7: Time Gate
P51_G7_TIME_GATE_ENABLED = True

# G8: Daily Cap & Cooldown
P51_G8_DAILY_SIGNAL_CAP = 3
P51_G8_SIGNAL_COOLDOWN_MINUTES = 45 # G8.1 standard cooldown
P51_G8_COOLDOWN_ON_SIGNAL_ADD = True # G8.3 immediate trigger

# G9: Math-First Momentum Logic (Section 2.5)
P61_G9_MATH_LOGIC_ENABLED = True
P61_G9_BYPASS_SD_THRESHOLD = 3.0     # Leung & Li: Extreme stretch = instant pass
P61_G9_ACCEL_REJECT_THRESHOLD = 2.0  # Reject if move-accel > 2.0% per 15m
P61_G9_STALL_PASS_THRESHOLD = 1.0    # Allow if move-accel < 1.0% (stall)

# PHASE 58: G12 VALIDATION HARDENING
P58_G12_USE_CANDLE_CLOSE: bool = True

# PHASE 60: MATHEMATICAL HARDENING
P60_G4_STRUCTURAL_FALLBACK_GAIN: float = 10.0  # 10% absolute gain floor
P60_G5_SPEAR_VOL_CLIMAX_MULT:    float = 3.0   # 3x Volume Climax

# G10: Spread & Confirmation
P51_G10_MAX_SPREAD_PCT = 0.004 # 0.4% (PRD: Downgrade to CAUTIOUS, not block)
P51_G10_TICK_OFFSET = 2 # Execute 2 ticks better than current LTP

# G11: Dynamic Timeout
P51_G11_MIN_REMAINING_MINUTES = 15

# G12: Invalidation Buffer
P51_G12_INVALIDATION_BUFFER_PCT = 0.002 # 0.2%

# ============================================================================
# PHASE 55 — GATE PRECISION HARDENING
# ============================================================================

# G5 Gate B: All-day high proximity tolerance
# Murphy: "A 2-5 tick violation of a key level is normal noise."
# 0.05% (old) = 2 ticks on most NSE stocks = too strict.
# 0.3% = institutional "close enough" zone.
P55_G5_GATE_B_DAY_HIGH_TOLERANCE: float = 0.003  # 0.3%

# G5 Gate C: Volume fade lookback window
# Wyckoff: compare to the CLIMAX candle, not a 5-bar trailing average.
# 15 candles captures the volume surge; 5 candles was only measuring
# already-normalised volume, making fade_ratio artificially high.
P55_G5_VOL_FADE_LOOKBACK: int = 15  # was hardcoded 5

# G6 RSI Divergence: Minimum lookback candles
# Murphy/Wilder: bearish RSI divergence requires 20-30 bars minimum.
# 10 candles (old) = 10 minutes = pure noise.
P55_G6_RSI_DIVERGENCE_WINDOW: int = 25  # was hardcoded 10

# G9 HTF: Use proper pivot swing high detection (vs raw candle comparison)
# Murphy: "A Lower High is a swing high that terminated below the prior swing high."
# Raw adjacent candle comparison produces false Lower High signals in uptrends.
P55_G9_USE_PIVOT_HIGH_DETECTION: bool = True

# G13: Risk & Reward (Phase 51 Hardening)
P51_SL_ATR_MULTIPLIER = 0.5
P51_SL_MIN_TICK_BUFFER = 3
P51_TP1_ATR_MULT = 1.5
P51_TP2_ATR_MULT = 2.5
P51_TP3_ATR_MULT = 3.5
P51_TP3_TRAIL_ATR_MULT = 1.0

# PHASE 65: AMT & QUANTITATIVE EVOLUTION
P65_AMT_ENABLED: bool = True
P65_G7_SAFE_TRADE_START: str = "10:00"
P65_G7_CLIMAX_WINDOW_START: str = "09:30"
P65_G7_CLIMAX_SD_THRESHOLD: float = 3.0
P65_G7_VOLUME_Z_SCORE_THRESHOLD: float = 2.0

P65_G1_NET_GAIN_THRESHOLD: float = 7.5  # Lowered from 9.0
P65_G1_AMT_REQUIRED_BELOW_9: bool = True
P65_G1_TIME_SINCE_HIGH_CANDLES: int = 45 # Extended from 20

# PHASE 66: ADAPTIVE GATES & SNAPSHOTS
P66_ADAPTIVE_G1_ENABLED: bool = True
P66_G1_ROTATION_THRESHOLD_PCT: float = 0.030  # 3.0% retrace allowed on decay
P66_G4_DECAY_CONFIRMATION_SEC: int = 120      # 2-minute confirmation window
P66_G4_DECAY_SD_THRESHOLD: float = 2.5        # Minimum SD for adaptive softening

# ============================================================================
# PHASE 52 — PARTIAL EXIT ENGINE & HUMAN INTERVENTION SAFETY
# ============================================================================

P52_PARTIAL_EXIT_ENABLED: bool = True      # Master: enables 40/40/20 TP logic
P52_BREAKEVEN_AFTER_TP1: bool = True       # Move SL to entry after TP1 hit
P52_SL_MOVE_AFTER_TP2: bool = True         # Move SL to TP1 level after TP2 hit
P52_CLEANUP_ON_STOP_FOCUS: bool = True     # Cancel pending orders on stop_focus()
P52_HARD_STOP_RECONCILE_SECONDS: int = 30  # Active position SL poll interval (was 1800)

# ============================================================================
# PHASE 70: DYNAMIC ML CONFIGURATION OVERRIDE
# ============================================================================
import json
import logging
from pathlib import Path

# Master Safety Switch: Set this to True in 1 month when enough data is collected
P70_ML_DYNAMIC_OVERRIDE_ENABLED = False

if P70_ML_DYNAMIC_OVERRIDE_ENABLED:
    DYNAMIC_CONFIG_PATH = Path("data/ml/dynamic_config.json")
    if DYNAMIC_CONFIG_PATH.exists():
        try:
            with open(DYNAMIC_CONFIG_PATH, 'r') as f:
                dynamic_overrides = json.load(f)
                
            _overridden = 0
            import sys
            _current_module = sys.modules[__name__]
            for key, val in dynamic_overrides.items():
                if hasattr(_current_module, key):
                    setattr(_current_module, key, val)
                    _overridden += 1
                    
            if _overridden > 0:
                print(f"✅ Loaded {_overridden} ML-optimized parameters from dynamic_config.json")
                
        except Exception as e:
            print(f"❌ Failed to load dynamic_config.json: {e}")
