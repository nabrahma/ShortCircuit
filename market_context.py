"""
Market Context Module
Determines market regime (Trend Day vs Range Day) using Nifty/BankNifty.
Based on Murphy's principle: "Trade with the trend, not against it."
"""
import logging
from datetime import datetime, time

logger = logging.getLogger(__name__)

class MarketContext:
    """
    Analyzes broader market to determine if it's safe to take reversal trades.
    """
    
    def __init__(self, fyers, morning_high=None, morning_low=None):
        self.fyers = fyers
        self.regime = "UNKNOWN"
        self.msg = "Initializing..."
        
        # Cache for today's morning range
        self._morning_high = morning_high
        self._morning_low = morning_low
        self._morning_range = (morning_high - morning_low) if (morning_high and morning_low) else None
        self._cache_date = None
        
        # Phase 41.3: Dynamic Regime State
        self.last_regime = 'UNKNOWN'
        self.regime_change_time = None
        self.trend_duration_minutes = 0
        
        if self._morning_high:
            logger.info(f"✅ Market Context Initialized with Morning Range: {self._morning_low} - {self._morning_high}")
    
    def _get_index_data(self, symbol="NSE:NIFTY50-INDEX"):
        """Fetch intraday data for the index."""
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
        Calculate the first hour's range (9:15 - 10:15).
        This establishes the reference for trend detection.
        """
        if not candles:
            return None, None, None
        
        # Filter candles from first hour
        morning_candles = []
        for c in candles:
            ts = datetime.fromtimestamp(c[0])
            if ts.time() <= time(10, 15):
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
        
        # Calculate morning range (cache it for the day)
        today = datetime.now().date()
        if self._cache_date != today:
            self._morning_high, self._morning_low, self._morning_range = \
                self._calculate_morning_range(candles)
            self._cache_date = today
        
        if self._morning_range is None or self._morning_range == 0:
            return "RANGE", "Morning range not established yet"
        
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
        now = datetime.now().time()
        
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
        
        # Also block during lunch chop (low volume = false signals)
        if phase == "LUNCH":
            return False, f"BLOCKED: {phase} - No signals during lunch (12:00-13:00)"
        
        # More lenient during afternoon/EOD
        if phase in ["AFTERNOON_TREND", "EOD_REVERSION"]:
            return True, f"Time: {phase} - {recommendation}"
        
        # TREND_ESTABLISHMENT phase (10:00-11:30): Check market regime
        regime, _ = self.get_market_regime()
        
        if regime == "TREND_UP":
            return False, f"BLOCKED: {phase} + TREND_UP - Wait for afternoon reversal window"
        
        return True, f"Time: {phase} - {recommendation}"

