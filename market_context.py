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

    # ── Phase 61: G7 Consolidation & Caching ────────────────────
    
    def _get_index_data_cached(self, symbol=None):
        """Fetch index candles with a 5-minute TTL cache."""
        if symbol is None: symbol = self.nifty_symbol
        
        now = _time.time()
        if not hasattr(self, '_index_cache'):
            self._index_cache = {}
            self._index_cache_time = {}
            
        # Cache hit?
        if symbol in self._index_cache and (now - self._index_cache_time.get(symbol, 0)) < 300:
            return self._index_cache[symbol]
            
        # Cache miss
        today = datetime.now(IST).strftime("%Y-%m-%d")
        data = {
            "symbol": symbol,
            "resolution": "5",
            "date_format": "1",
            "range_from": today,
            "range_to": today,
            "cont_flag": "1"
        }
        
        try:
            response = self.fyers.history(data=data)
            if response.get('s') == 'ok' and response.get('candles'):
                self._index_cache[symbol] = response['candles']
                self._index_cache_time[symbol] = now
                return response['candles']
        except Exception as e:
            logger.error(f"Failed to fetch index data for {symbol}: {e}")
        
        return self._index_cache.get(symbol) # Fallback to expired cache if API fails

    def evaluate_g7(self) -> tuple[bool, str]:
        """
        Consolidated Gate 7: Market Regime + Time Filters.
        Replaces legacy should_allow_short and is_favorable_time_for_shorts.
        Returns: (allowed, reason)
        """
        now_ist = datetime.now(IST).time()
        
        # 1. TIME GATE: Opening Volatility
        if now_ist < time(10, 0):
            return False, "BLOCKED [G7]: Opening Volatility (9:15-10:00)"
            
        # 2. TIME GATE: EOD Cutoff
        if config.PHASE_51_ENABLED and config.P51_G7_TIME_GATE_ENABLED:
            if now_ist >= time(15, 10):
                return False, "BLOCKED [G7]: EOD Cutoff (after 15:10)"

        # 3. REGIME DETECTION: Nifty Trend
        candles = self._get_index_data_cached(self.nifty_symbol)
        if not candles:
            return True, "ALLOWED [G7]: No index data, bypassing regime"
            
        self._refresh_morning_range_if_needed()
        if not self.morning_range_valid:
            return True, "ALLOWED [G7]: Morning range unavailable, bypassing regime"

        current_close = candles[-1][4]
        conf = config.MARKET_REGIME_CONFIG
        move_pct = (current_close - self._morning_high) / self._morning_high
        
        # Update Internal Regime State for logging
        new_regime = "RANGE"
        if move_pct > conf['strong_trend_threshold']:
            new_regime = "TREND_UP"
        elif move_pct < -0.005:
            new_regime = "TREND_DOWN"
            
        if new_regime != self.last_regime:
            self.last_regime = new_regime
            self.regime_change_time = datetime.now()
            logger.info(f"📊 REGIME CHANGE: {new_regime} | Move: {move_pct:.2%}")

        # STRONG TREND (>1.5%) -> Hard Block
        if move_pct > conf['strong_trend_threshold']:
            return False, f"BLOCKED [G7]: Strong Trend Up (+{move_pct:.2%})"

        # 4. DEFAULT: Range Day or Trend Down
        return True, "OK [G7]: Market in Range / Trend Down"

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
