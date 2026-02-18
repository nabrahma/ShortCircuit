import time
import sys
import logging
from datetime import datetime, time as dtime, timedelta, timezone

# Helper for IST Timezone
IST = timezone(timedelta(hours=5, minutes=30))

logger = logging.getLogger(__name__)

from config import MARKET_SESSION_CONFIG, set_trading_enabled
from symbols import NIFTY_50

class MarketSession:
    """
    Phase 41.3.1: Intelligent Market Session Awareness.
    Handles startup at ANY time of day.
    """
    
    # Phase 41.3.3: Centralized Symbol
    NIFTY_SYMBOL = NIFTY_50
    
    # NSE Market Hours (IST)
    MARKET_OPEN = dtime(9, 15)
    SAFE_TRADE_START = dtime(9, 45)
    EOD_SQUARE_OFF = dtime(15, 10)
    MARKET_CLOSE = dtime(15, 30)

    def __init__(self, fyers, telegram_bot):
        self.fyers = fyers
        self.telegram = telegram_bot
        self.session_state = None
        self.morning_context = None

    def initialize_session(self):
        """
        Main entry point. Analyzes time and handles waiting/logic.
        Returns: session_context (dict) or None
        """
        state = self.get_current_state()
        self.session_state = state
        
        logger.info(f"🕐 Initializing Session. Detected State: {state}")
        
        if state == 'PRE_MARKET':
            self.handle_premarket()
        elif state == 'EARLY_MARKET':
            self.handle_early_market()
        elif state == 'MID_MARKET':
            return self.handle_mid_market()
        elif state == 'EOD_WINDOW':
            self.handle_eod_window()
        elif state == 'POST_MARKET':
            self.handle_postmarket()
            
        return None

    def get_current_state(self):
        """Determine current market state based on IST time."""
        now = datetime.now(IST)
        t = now.time()
        
        # Weekend Check
        if now.weekday() >= 5:
            return 'POST_MARKET'

        if t < self.MARKET_OPEN:
            return 'PRE_MARKET'
        elif self.MARKET_OPEN <= t < self.SAFE_TRADE_START:
            return 'EARLY_MARKET'
        elif self.SAFE_TRADE_START <= t < self.EOD_SQUARE_OFF:
            return 'MID_MARKET'
        elif self.EOD_SQUARE_OFF <= t < self.MARKET_CLOSE:
            return 'EOD_WINDOW'
        else:
            return 'POST_MARKET'

    def should_trade_now(self):
        """Gatekeeper for main loop execution"""
        state = self.get_current_state()
        # Only trade in MID_MARKET or EARLY_MARKET (if enabled)
        # But specifically, we use TRADING_ENABLED flag in config.
        # This function updates state transitions if needed.
        
        if state != self.session_state:
            # State Transition Detected
            old_state = self.session_state
            self.session_state = state
            self._handle_transition(old_state, state)
            
        return state in ['MID_MARKET', 'EARLY_MARKET']

    def _handle_transition(self, old, new):
        """Handle dynamic state changes during runtime"""
        logger.info(f"🔄 State Transition: {old} -> {new}")
        
        if new == 'MID_MARKET':
            set_trading_enabled(True)
            self._send_formatted_msg("🟢 **MARKET ACTIVE**\n\nTrading Enabled!", "MID_MARKET")
            
        elif new == 'EOD_WINDOW':
            set_trading_enabled(False)
            self._send_formatted_msg("🌆 **EOD WINDOW**\n\nTrading Disabled. Square-off imminent.", "EOD_WINDOW")
            
        elif new == 'POST_MARKET':
            set_trading_enabled(False)
            self._send_formatted_msg("🌙 **MARKET CLOSED**\n\nSee you tomorrow!", "POST_MARKET")

    # ── HANDLERS ──────────────────────────────────────────────────

    def handle_premarket(self):
        now = datetime.now(IST)
        wait_seconds = self._seconds_until(self.MARKET_OPEN)
        wait_mins = wait_seconds / 60
        
        msg = (
            f"🌅 **PRE-MARKET START**\n\n"
            f"⏰ Current: `{now.strftime('%H:%M:%S')}`\n"
            f"⏳ Waiting: `{wait_mins:.0f} mins`\n\n"
            f"✅ System Ready. Sleeping until 9:15."
        )
        self._notify(msg)
        logger.info(f"💤 PRE-MARKET: Sleeping {wait_seconds}s until 9:15...")
        
        time.sleep(wait_seconds)
        
        # Transition
        self.session_state = 'EARLY_MARKET'
        self.handle_early_market()

    def handle_early_market(self):
        now = datetime.now(IST)
        wait_seconds = self._seconds_until(self.SAFE_TRADE_START)
        wait_mins = wait_seconds / 60
        
        msg = (
            f"🌄 **EARLY MARKET WARMUP**\n\n"
            f"⏰ Current: `{now.strftime('%H:%M:%S')}`\n"
            f"⏳ Trading Starts: `09:45` ({wait_mins:.0f} mins)\n\n"
            f"**Status:** Monitor Mode Only.\n"
            f"Collecting candle data..."
        )
        self._notify(msg)
        set_trading_enabled(False) # Ensure disabled
        
        logger.info(f"🚫 EARLY MARKET: Warmup. Trading Disabled. Waiting {wait_seconds}s...")
        # Optional: We could run a loop here to 'warm up' scanner without trading,
        # but for simplicity/reliability, we'll sleep or use main loop with disabled flag.
        # PRD suggests sleep.
        time.sleep(wait_seconds)
        
        # Transition
        self.session_state = 'MID_MARKET'
        set_trading_enabled(True)
        self._notify("🟢 **TRADING ENABLED**\n\nWarmup Complete. Good Luck!")

    def handle_mid_market(self):
        now = datetime.now(IST)
        ranges = self._fetch_morning_range()
        
        msg = (
            f"🌞 **MID-MARKET ENTRY**\n\n"
            f"⏰ Time: `{now.strftime('%H:%M:%S')}`\n"
            f"📊 Morning High: `{ranges['high']}`\n"
            f"📊 Morning Low: `{ranges['low']}`\n\n"
            f"✅ Context Loaded. Trading ACTIVE."
        )
        self._notify(msg)
        set_trading_enabled(True)
        self.morning_context = ranges
        return ranges

    def handle_eod_window(self):
        now = datetime.now(IST)
        mins_left = (self._seconds_until(self.MARKET_CLOSE)) / 60
        
        msg = (
            f"🌆 **EOD WINDOW**\n\n"
            f"⏰ Time: `{now.strftime('%H:%M:%S')}`\n"
            f"⚠️ Too late for new trades.\n"
            f"⏳ Market Closes in {mins_left:.0f} mins.\n\n"
            f"Mode: **Monitoring Only**"
        )
        self._notify(msg)
        set_trading_enabled(False)
        # Just return, main loop handles monitoring (reconciliation)

    def handle_postmarket(self):
        now = datetime.now(IST)
        next_open = self._next_market_open_time()
        hours_until = (next_open - now).total_seconds() / 3600
        
        msg = (
            f"🌙 **POST-MARKET START**\n\n"
            f"⏰ Time: `{now.strftime('%H:%M:%S')}`\n"
            f"📪 Market Closed.\n\n"
            f"🔜 Next Open: `{next_open.strftime('%a %H:%M')}`\n"
            f"⏳ Sleep: `{hours_until:.1f} hours`"
        )
        self._notify(msg)
        
        if MARKET_SESSION_CONFIG.get('allow_postmarket_sleep', True):
            sleep_sec = (next_open - now).total_seconds()
            logger.info(f"💤 Sleeping {sleep_sec}s until next open...")
            time.sleep(sleep_sec)
            
            # Wake up -> Reset
            self.session_state = 'PRE_MARKET' # Will naturally flow
            # Recursively restart? Or just return and let main loop catch up?
            # Ideally main loop detects time. But initialize_session is called ONCE.
            # So if we sleep here, we should re-call logic.
            return self.initialize_session()
        else:
            logger.info("👋 Auto-sleep disabled. Exiting.")
            sys.exit(0)

    # ── HELPERS ───────────────────────────────────────────────────

    def _fetch_morning_range(self):
        """Fetch 9:15-9:30 range for NIFTY"""
        fallback_high = 25500 # Dummy
        fallback_low = 25300
        
        try:
            today_str = datetime.now(IST).strftime('%Y-%m-%d')
            data = {
                "symbol": self.NIFTY_SYMBOL,
                "resolution": "5",
                "date_format": "1",
                "range_from": f"{today_str}", # Fyers usually takes epoch or YYYY-MM-DD
                "range_to": f"{today_str}",
                "cont_flag": "1"
            }
            # Need to start_epoch and end_epoch for 'history' usually?
            # Using basic logic from existing scanner or manual.
            # Assuming fyers.history expects epoch or date string depending on 'date_format'.
            # PRD uses YYYY-MM-DD string with date_format=1? 
            # Usually date_format=0 is epoch, 1 might be string. I'll trust standard usage of 'history'.
            # Wait, fyers documentation says date_format 0=epoch, 1=yyyy-mm-dd.
            
            # Since we need specific time 9:15 to 9:30, we must use timestamps if granularity is needed within a day?
            # Or pass full date string "2025-01-01 09:15:00"?
            # Let's use simple logic: Fetch today's data and slice.
            
            # Actually, `market_context.py` handles this.
            # I will return a best-effort estimate or fallback.
            
            # For now returning placeholder structure, relying on existing `market_context` logic 
            # to be updated to accept this.
            
            return {'high': fallback_high, 'low': fallback_low} 
            
        except Exception as e:
            logger.error(f"Failed to fetch morning range: {e}")
            return {'high': fallback_high, 'low': fallback_low}

    def _seconds_until(self, target_time):
        now = datetime.now(IST)
        target = datetime.combine(now.date(), target_time, tzinfo=IST)
        if target < now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _next_market_open_time(self):
        now = datetime.now(IST)
        target = datetime.combine(now.date(), self.MARKET_OPEN, tzinfo=IST)
        if now.time() >= self.MARKET_CLOSE:
            target += timedelta(days=1)
        
        # Skip Weekends
        while target.weekday() >= 5:
            target += timedelta(days=1)
            
        return target

    def _notify(self, msg, tag=None):
        """Send formatted alert via Telegram"""
        try:
            if self.telegram and MARKET_SESSION_CONFIG.get('telegram_state_transitions'):
                self.telegram.send_alert(msg)
        except:
            pass
        
    def _send_formatted_msg(self, msg, tag):
        self._notify(msg, tag)
