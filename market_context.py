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
    
    def __init__(self, fyers):
        self.fyers = fyers
        self.nifty_symbol = "NSE:NIFTY50-INDEX"
        self.banknifty_symbol = "NSE:NIFTYBANK-INDEX"
        
        # Cache for today's morning range
        self._morning_high = None
        self._morning_low = None
        self._morning_range = None
        self._cache_date = None
    
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
        
        # Trend detection thresholds
        # If price is 0.5x range ABOVE morning high -> Trend Up
        # If price is 0.5x range BELOW morning low -> Trend Down
        extension_threshold = 0.5 * self._morning_range
        
        if current_close > self._morning_high + extension_threshold:
            regime = "TREND_UP"
            msg = f"NIFTY trending UP (Current: {current_close:.0f}, Morning High: {self._morning_high:.0f})"
        elif current_close < self._morning_low - extension_threshold:
            regime = "TREND_DOWN"
            msg = f"NIFTY trending DOWN (Current: {current_close:.0f}, Morning Low: {self._morning_low:.0f})"
        else:
            regime = "RANGE"
            msg = f"NIFTY in RANGE ({self._morning_low:.0f} - {self._morning_high:.0f})"
        
        logger.info(f"Market Regime: {regime} | {msg}")
        return regime, msg
    
    def should_allow_short(self):
        """
        Simple check: Should we allow SHORT signals right now?
        
        Returns:
            tuple: (allowed, reason)
        """
        regime, msg = self.get_market_regime()
        
        if regime == "TREND_UP":
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

