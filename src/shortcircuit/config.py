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
# Armed on boot BY DESIGN — do not change to False. Telegram is unreliable on this
# connection, so requiring a manual /auto on each morning meant missed sessions.
# Trading is still gated by TRADING_ENABLED (below), which MarketSession only turns
# on at 09:30 IST. Disarm at runtime with /auto off; nothing disarms it automatically.
AUTO_MODE = True            # Controls if the bot auto-executes trades (toggle via Telegram)
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
# Restored to 45 on 2026-08-30. The reasoning that set this to 0 rested on two
# live days and one trade (NSE:BAJAJELEC-EQ) that needed 67 minutes. Replaying
# all 46 LIVE trades since 11 Jun against real 1-minute candles put the 45-minute
# cap ahead of no cap in every phase: +2.75pp over the green era, +3.61pp across
# 7-11 Aug, and level afterwards. 0 disables the exit entirely.
MAX_HOLD_TIME_MINUTES = 45

# ============================================================================
# 3. SCANNER & G5 STRETCH CONSTANTS
# ============================================================================
# Gain Floors & Limits
SCANNER_GAIN_MIN_PCT: float = 7.5  # Phase 65: Synchronized with P65_G1 floor
SCANNER_GAIN_MAX_PCT: float = 18.0 # Protection against upper-circuit runners
# 2026-08-12: halved from 333,333. NSE:ORISSAMINE-EQ ran and broke down that
# morning but only crossed the old floor at 11:56 IST, ~26 min after the move,
# because it is a low-float name whose whole-day volume is small. Lower floor =
# illiquid movers become visible in time; it also admits thinner books, so watch
# slippage. SCANNER_MIN_LTP is the remaining guard against manipulation vehicles.
SCANNER_MIN_VOLUME:   int   = 161616
SCANNER_MIN_LTP:      float = 40.0   # Filter sub-₹40 manipulation vehicles

# Minimum intraday (MIS) leverage the broker must grant before a symbol is worth
# analysing at all. The operator trades 4x and 5x names only.
#
# Measured against the live margin API on 2026-08-31 over that session's 22
# candidates, the distribution is strictly bimodal:
#     12 symbols at 4.0x / 4.99x        10 symbols at exactly 1.0x
# Nothing in between. At 1.0x the required margin equals the share price, which
# is the broker refusing MIS: NSE:SHIPROCKET-EQ needed ₹138.93 on a ₹138.89
# share, passed all six gates, and was rejected at order time with
# "RED:RULE:{Allowed Basket} in Basket NSE.MIS.NSE_MIS_BASKET".
#
# 3.5 sits in the empty gap, so it selects exactly the 4x/5x cohort and is
# insensitive to rounding. There is deliberately no threshold to tune here —
# picking a number inside a populated range is what produced a zero-trade
# session last time (see cd178cc). An unknown reading NEVER blocks: the scanner
# fails open and the broker rejection remains the backstop.
SCANNER_MIN_LEVERAGE: float = 3.5

# Wall-clock cap on the leverage screen per scan. Readings are cached per symbol
# for the session, so this only binds on a morning with many unseen movers.
# Past the budget, remaining symbols pass through unscreened rather than
# delaying the scan — main.py times a scan out at 90s.
SCANNER_LEVERAGE_BUDGET_SECONDS: float = 15.0
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
# C4/C5 horizons restored to their June values on 2026-08-30.
# b11b773 (23 Jul) shortened both "to align with fast momentum spikes". That was
# three weeks after the 30 Jun break and during the flat/losing stretch, so it did
# not cause the drawdown — but June's +38.9% was earned at 15 and 25, and neither
# setting has ever been measured against the other.
STRATEGY_VOL_FADE_LOOKBACK: int = 15         # Candles to look back for volume baseline
STRATEGY_RSI_DIVERGENCE_WINDOW: int = 25     # Window for swing-based RSI divergence check
STRATEGY_MOMENTUM_DECAY_RATIO: float = 0.85  # Fast slope must be < slow * this ratio

# ============================================================================
# 6. EXIT ENGINE & RISK MULTIPLIERS
# ============================================================================
SL_ATR_MULTIPLIER = 0.5
SL_MIN_TICK_BUFFER = 3

P52_CLEANUP_ON_STOP_FOCUS: bool = True 

# ── Take-profit engine (restored 2026-08-30) ────────────────────────────────
# Removed on 6 Aug by f6eae88 on the strength of two live days. Replaying all
# 46 LIVE trades since 11 Jun on real 1-minute candles reversed that read: over
# the green era, running no take-profit scored -3.10 against +3.15 for the
# single midpoint TP and +3.27 for the scale-out. Losing trades are untouched
# by any of it — 11 of 15 August trades hit the stop first and score identically
# under every policy. The TP only ever changes winners.
#
#   'SCALE'  — 50% at the midpoint, remainder runs to the VWAP target.
#              This is the pre-30-Jun policy and the best of the replayed set.
#   'SINGLE' — 100% at the midpoint. The 30 Jun - 6 Aug policy.
#   'OFF'    — no take-profit; stop-loss and EOD only (6-30 Aug behaviour).
#
# Caveat worth keeping in view: n=46, and the paired confidence intervals span
# zero. The ranking is a point estimate, not a proven result.
TP_MODE: str = 'SCALE'

# Moves the stop to breakeven once the partial fills under TP_MODE='SCALE'.
# This is the one piece of the restored engine that contradicts the standing
# "no breakeven SL" rule, so it is on its own switch. Setting it False keeps
# the scale-out and leaves the original stop where it is.
P52_BREAKEVEN_AFTER_TP1: bool = True

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

# ── VWAP anchor (restored to ROLLING 2026-08-30) ────────────────────────────
# features.enrich_dataframe computes a cumulative VWAP over whatever frame it
# receives, so the frame length IS the anchor.
#
#   'ROLLING' — hand it VWAP_ROLLING_BARS bars. The anchor slides forward each
#               minute, so C1 measures stretch against roughly the last 100
#               minutes: a fresh impulsive extension.
#   'SESSION' — hand it the whole session. C1 measures distance from the day's
#               average, which stays elevated all day on a strong trender.
#
# SESSION is the textbook-correct reading and 90a2998 switched to it on 12 Aug
# to fix a real defect: NSE:ORISSAMINE-EQ read -1.52 SD, below VWAP, while the
# session anchor had it above. That defect is real and returns with ROLLING.
#
# It is also what the entire profitable run was measured on. The 11 LIVE trades
# after the switch averaged 0.572 MFE against 1.088 in the 4 before it, and no
# exit policy in the replay rescues that window — the entries themselves got
# worse. Same shape as the Dalton VAH reverted in July: correct by the book,
# load-bearing in its broken form.
#
# Sample sizes are 4 and 11. This is a deliberate bet, not a demonstrated fact.
VWAP_ANCHOR_MODE: str = 'ROLLING'
VWAP_ROLLING_BARS: int = 100

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
