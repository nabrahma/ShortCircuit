"""
Market Context Module
Determines market regime (Trend Day vs Range Day) using Nifty/BankNifty.
Based on Murphy's principle: "Trade with the trend, not against it."
"""
import logging
import time as _time
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from symbols import NIFTY_50, validate_symbol
import config

logger = logging.getLogger(__name__)
IST = ZoneInfo("Asia/Kolkata")

class MarketContext:
    """
    Analyzes broader market to determine if it's safe to take reversal trades.
    """
    
    # Phase 41.3.3: Centralized Symbol Handling
    NIFTY_SYMBOL = NIFTY_50
    
    def __init__(self, fyers, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.regime = "UNKNOWN"
        self.msg = "Initializing..."
        
        # Phase 41.3.3: Explicit Symbol Initialization
        self.nifty_symbol = self.NIFTY_SYMBOL
        
        # Validate symbol
        if not validate_symbol(self.nifty_symbol):
             logger.error(f"Invalid NIFTY Symbol: {self.nifty_symbol}")
             raise ValueError(f"Invalid NIFTY Symbol: {self.nifty_symbol}")

        # Cache for today's morning range
        self._morning_high = morning_high
        self._morning_low = morning_low
        self._morning_range = (morning_high - morning_low) if (morning_high and morning_low) else None
        self._cache_date = None
        self.morning_range_valid = bool(
            self._morning_high and self._morning_low and self._morning_range and self._morning_range > 0
        )
        
        # Phase 41.3: Dynamic Regime State
        self.last_regime = 'UNKNOWN'
        self.regime_change_time = None
        self.trend_duration_minutes = 0
        self._circuit_touched_today = set() # Phase 51: G3 Blacklist (Session-permanent)
        self._circuit_blacklist_date = datetime.now(IST).date()

        
        if self._morning_high:
            logger.info(f"✅ Market Context Initialized with Morning Range: {self._morning_low} - {self._morning_high}")
            logger.info(f"   Index: {self.nifty_symbol}")

    @property
    def morning_high(self) -> float:
        return float(self._morning_high or 0.0)

    @property
    def morning_low(self) -> float:
        return float(self._morning_low or 0.0)

    def _fetch_morning_range_from_rest(self):
        """
        Fetch NIFTY morning range (09:15–09:45 IST) from REST 1-minute candles.
        Returns tuple(high, low). On failure, returns (0.0, 0.0).
        """
        now_ist = datetime.now(IST)
        today = now_ist.date()
        five_days_ago = today - timedelta(days=5)

        data = {
            "symbol": self.nifty_symbol,
            "resolution": "1",
            "date_format": "1",
            "range_from": five_days_ago.strftime("%Y-%m-%d"),
            "range_to": today.strftime("%Y-%m-%d"),
            "cont_flag": "1",
        }

        try:
            response = self.fyers.history(data=data)
        except Exception as e:
            logger.critical(f"[MarketContext] Morning range REST fetch exception: {e}")
            return 0.0, 0.0

        candles = response.get("candles") if isinstance(response, dict) else None
        if response.get("s") != "ok" or not candles:
            logger.critical(
                "[MarketContext] Morning range REST fetch failed: status=%s code=%s",
                response.get("s"),
                response.get("code"),
            )
            return 0.0, 0.0

        morning_start = time(9, 15)
        morning_end = time(9, 45)
        _IST = ZoneInfo("Asia/Kolkata")
        market_open = int(datetime(today.year, today.month, today.day, 9, 15, tzinfo=_IST).timestamp())
        warmup_end = int(datetime(today.year, today.month, today.day, 9, 45, tzinfo=_IST).timestamp())

        morning_candles = []
        for c in candles:
            ts_ist = datetime.fromtimestamp(c[0], tz=timezone.utc).astimezone(IST)
            if ts_ist.date() == today and market_open <= c[0] <= warmup_end:
                morning_candles.append(c)

        if not morning_candles:
            logger.warning(
                "[MarketContext] No 09:15–09:45 candles found; falling back to all today's intraday candles."
            )
            all_today = []
            for c in candles:
                ts_ist = datetime.fromtimestamp(c[0], tz=timezone.utc).astimezone(IST)
                if ts_ist.date() == today and ts_ist.time() >= morning_start:
                    all_today.append(c)
            if not all_today:
                return 0.0, 0.0
            morning_candles = all_today

        morning_high = max(c[2] for c in morning_candles)
        morning_low = min(c[3] for c in morning_candles)
        logger.info(
            "[MarketContext] ✅ Morning range fetched via REST: High=%s Low=%s (%s candles)",
            round(morning_high, 2),
            round(morning_low, 2),
            len(morning_candles),
        )
        return morning_high, morning_low

    def _refresh_morning_range_if_needed(self):
        """
        Daily morning range initialization.
        Always uses REST to avoid startup dependence on WS cache state.
        """
        now = _time.time()
        if not hasattr(self, '_last_range_fetch_time'):
            self._last_range_fetch_time = 0.0
        if now - self._last_range_fetch_time < 300:
            return
        self._last_range_fetch_time = now

        today = datetime.now(IST).date()
        if self._cache_date == today and self.morning_range_valid:
            return

        high, low = self._fetch_morning_range_from_rest()
        self._cache_date = today

        if high > 0 and low > 0 and high > low:
            self._morning_high = high
            self._morning_low = low
            self._morning_range = high - low
            self.morning_range_valid = True
            logger.info(
                "[MarketContext] ✅ Initialized | Range: %s - %s",
                round(self._morning_low, 2),
                round(self._morning_high, 2),
            )
            return

        self._morning_high = 0.0
        self._morning_low = 0.0
        self._morning_range = 0.0
        self.morning_range_valid = False
        logger.critical(
            "[MarketContext] ⚠️ Morning range unavailable — range-dependent checks are bypassed."
        )

    
    def _get_index_data(self, symbol=None):
        """Fetch intraday data for the index."""
        if symbol is None:
            symbol = self.nifty_symbol
            
        today = datetime.now().strftime("%Y-%m-%d")
        
        data = {
            "symbol": symbol,
            "resolution": "5",  # 5-minute for smoother regime detection
            "date_format": "1",
            "range_from": today,
            "range_to": today,
            "cont_flag": "1"
        }
        
        try:
            response = self.fyers.history(data=data)
            if response.get('s') == 'ok' and response.get('candles'):
                return response['candles']
        except Exception as e:
            logger.error(f"Failed to fetch index data: {e}")
        
        return None
    
    def _calculate_morning_range(self, candles):
        """
        Calculate morning range (09:15 - 09:45 IST) from provided candles.
        This establishes the reference for trend detection.
        """
        if not candles:
            return None, None, None
        
        # Filter candles from 09:15 to 09:45 IST.
        morning_candles = []
        for c in candles:
            ts = datetime.fromtimestamp(c[0], tz=timezone.utc).astimezone(IST)
            if time(9, 15) <= ts.time() <= time(9, 45):
                morning_candles.append(c)
        
        if not morning_candles:
            return None, None, None
        
        morning_high = max(c[2] for c in morning_candles)  # c[2] = high
        morning_low = min(c[3] for c in morning_candles)   # c[3] = low
        morning_range = morning_high - morning_low
        
        return morning_high, morning_low, morning_range
    
    def get_market_regime(self):
        """
        Determine if it's a TREND DAY or RANGE DAY.
        
        Returns:
            tuple: (regime, message)
                - regime: "TREND_UP", "TREND_DOWN", or "RANGE"
                - message: Explanation for logging
        """
        candles = self._get_index_data(self.nifty_symbol)
        
        if not candles:
            logger.warning("Could not fetch Nifty data, assuming RANGE")
            return "RANGE", "No index data available"
        
        # Ensure morning range for this session is sourced from REST.
        self._refresh_morning_range_if_needed()
        if not self.morning_range_valid:
            return "RANGE", "Morning range unavailable (range-dependent gates bypassed)"
        
        # Get current price
        current_close = candles[-1][4]  # c[4] = close
        
        # ── PHASE 41.3: DYNAMIC THRESHOLDS ────────────────────
        conf = config.MARKET_REGIME_CONFIG
        
        # Calculate deviation from morning range
        # Only focusing on TREND UP for blocking shorts
        move_pct = (current_close - self._morning_high) / self._morning_high
        
        new_regime = "RANGE"
        msg = f"NIFTY in RANGE ({self._morning_low:.0f} - {self._morning_high:.0f})"
        
        # 1. STRONG TREND (>1.0%) -> Immediate Block
        if move_pct > conf['strong_trend_threshold']:
            new_regime = "TREND_UP"
            msg = f"STRONG TREND UP (+{move_pct:.2%}) > 1.0%"
            
        # 2. MODERATE TREND (>0.5%) -> Check Duration
        elif move_pct > conf['moderate_trend_threshold']:
            if self.last_regime == "TREND_UP":
                # Check momentum decay
                duration = (datetime.now() - self.regime_change_time).seconds / 60 if self.regime_change_time else 0
                if duration > conf['momentum_decay_minutes'] and move_pct < 0.008:
                     new_regime = "RANGE"
                     msg = "TREND UP -> RANGE (Decaying Momentum)"
                else:
                    new_regime = "TREND_UP"
                    msg = f"SUSTAINED TREND UP (+{move_pct:.2%}) > 0.5%"
            else:
                # Waiting for confirmation
                new_regime = "RANGE"
                msg = f"POTENTIAL TREND (+{move_pct:.2%}) - Waiting confirmation"
        
        # 3. DOWN TREND
        elif current_close < self._morning_low * 0.995: # Simple 0.5% below low
            new_regime = "TREND_DOWN"
            msg = "TREND DOWN"

        # Update State
        if new_regime != self.last_regime:
            self.last_regime = new_regime
            self.regime_change_time = datetime.now()
            logger.info(f"📊 REGIME CHANGE: {new_regime} | {msg}")
            
        return new_regime, msg
    
    def should_allow_short(self, symbol=None, pattern=None, stock_ltp=None):
        """
        Phase 41.3: Intelligent Regime Filter with Overrides.
        """
        regime, msg = self.get_market_regime()
        
        if regime == "TREND_UP":
            # CHECK OVERRIDES
            if symbol and pattern and stock_ltp:
                conf = config.MARKET_REGIME_CONFIG
                if pattern in conf['override_patterns']:
                    # Check Divergence: Stock is weak while Market is strong
                    # Mocking open price since we assume stock_ltp is current
                    # Ideally we need stock's open/close for % change. 
                    # Assuming caller might verify divergence?
                    # For now, just logging the attempt.
                    return False, f"BLOCKED: {msg} (Override logic requires stock % change data)"
                    
                    # NOTE: To implement divergence check properly, we need stock's change %.
                    # But `should_allow_short` signature in analyzer limits us. 
                    # Analyzer should handle the divergence check if pattern matches.
                    # For now, we STRICTLY follow the regime unless caller handles it.
            
            return False, f"BLOCKED: {msg} - No shorts on trend-up days"
        
        return True, f"OK: {msg}"
    
    def get_time_filter(self):
        """
        Time-of-day filter based on Volman's session analysis.
        
        Returns:
            tuple: (phase, recommendation)
        """
        now = datetime.now(IST).time()    # IST-explicit — safe on any server timezone
        
        if now < time(10, 0):
            return "OPENING", "Avoid - Opening volatility"
        elif now < time(11, 30):
            return "TREND_ESTABLISHMENT", "Caution - Let trends establish"
        elif now < time(13, 0):
            return "LUNCH", "Avoid - Low volume chop"
        elif now < time(14, 30):
            return "AFTERNOON_TREND", "OK - Trend continuation"
        else:
            return "EOD_REVERSION", "BEST - Mean reversion works here"
    
    def is_favorable_time_for_shorts(self):
        """
        Check if current time favors reversal trades.
        
        Phase 24.3: Stricter time filter based on Jan 29 analysis.
        - 09:41 AXISGOLD lost (early morning)
        - 09:55 GOLDADD lost (early morning)
        - 10:10 NAHARSPING WON (after 10 AM)
        
        Returns:
            tuple: (favorable, reason)
        """
        phase, recommendation = self.get_time_filter()
        
        # PHASE 24.3: BLOCK all signals before 10:00 AM
        # Opening hour has too much volatility - reversals get stopped out
        if phase == "OPENING":
            return False, f"BLOCKED: {phase} - No signals before 10:00 AM (opening volatility)"
        
        # Phase 57: Lunch block disabled per USER request (12:00-13:00)
        # if phase == "LUNCH":
        #     return False, f"BLOCKED: {phase} - No signals during lunch (12:00-13:00)"
        
        # Phase 51: G7 EOD Cutoff
        if config.PHASE_51_ENABLED and config.P51_G7_TIME_GATE_ENABLED:
            now_ist = datetime.now(IST).time()
            if now_ist >= time(15, 10):
                return False, "BLOCKED [G7]: EOD Cutoff (after 15:10)"

        
        # More lenient during afternoon/EOD
        if phase in ["AFTERNOON_TREND", "EOD_REVERSION"]:
            return True, f"Time: {phase} - {recommendation}"
        
        # TREND_ESTABLISHMENT phase (10:00-11:30): Check market regime
        regime, _ = self.get_market_regime()
        
        if regime == "TREND_UP":
            return False, f"BLOCKED: {phase} + TREND_UP - Wait for afternoon reversal window"
        
        return True, f"Time: {phase} - {recommendation}"

    # ── Phase 51: G3 Circuit Hitter Methods ────────────────────
    
    def mark_circuit_touched(self, symbol: str):
        """
        Mark a symbol as having touched circuit limits.
        Phase 51 [G3]: Session-permanent block.
        """
        self._refresh_circuit_blacklist_if_needed()
        self._circuit_touched_today.add(symbol)
        logger.warning(f"[G3] Symbol {symbol} marked as CIRCUIT HITTER. Blacklisted for remainder of session.")

    def is_circuit_hitter(self, symbol: str) -> bool:
        """
        Check if symbol is currently blacklisted due to circuit touch.
        Phase 51 [G3]: Session-permanent block.
        """
        self._refresh_circuit_blacklist_if_needed()
        return symbol in self._circuit_touched_today

    def _refresh_circuit_blacklist_if_needed(self):
        """Resets the circuit blacklist if a new day has started."""
        today = datetime.now(IST).date()
        if self._circuit_blacklist_date != today:
            self._circuit_touched_today.clear()
            self._circuit_blacklist_date = today
            logger.info("[G3] Daily Circuit Blacklist cleared for new session.")
