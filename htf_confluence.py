"""
Higher Timeframe Confluence Module
Checks 5m and 15m charts to confirm trend weakness before shorting.
Based on Murphy's principle: "The larger timeframe governs the smaller."
"""
import logging
import datetime
import pandas as pd

logger = logging.getLogger(__name__)

class HTFConfluence:
    """
    Analyzes higher timeframe charts to confirm trade direction.
    """
    
    def __init__(self, fyers):
        self.fyers = fyers
    
    def _get_htf_history(self, symbol, interval="15"):
        """
        Fetch higher timeframe data.
        
        Args:
            symbol: Stock symbol
            interval: "5" for 5-min, "15" for 15-min
        """
        today = datetime.date.today().strftime("%Y-%m-%d")
        
        data = {
            "symbol": symbol,
            "resolution": interval,
            "date_format": "1",
            "range_from": today,
            "range_to": today,
            "cont_flag": "1"
        }
        
        try:
            response = self.fyers.history(data=data)
            if response.get('s') == 'ok' and response.get('candles'):
                candles = response['candles']
                df = pd.DataFrame(candles, columns=['t', 'o', 'h', 'l', 'c', 'v'])
                df['t'] = pd.to_datetime(df['t'], unit='s')
                return df
        except Exception as e:
            logger.error(f"HTF data fetch failed for {symbol}: {e}")
        
        return None
    
    def check_15m_structure(self, symbol):
        """
        Check if 15-minute chart shows weakness for shorts.
        
        Looking for:
        - Lower High: Last high is lower than previous high
        - Distribution: Volume declining on up moves
        
        Returns:
            tuple: (has_weakness, message)
        """
        df = self._get_htf_history(symbol, interval="15")
        
        if df is None or len(df) < 4:
            # Phase 24.1: BLOCK signals without HTF confirmation
            # Jan 29 showed: "Insufficient HTF" signals lost, proper confirmations won
            return False, "Insufficient HTF data - BLOCKED (need 15m confirmation)"
        
        # Get last 4 pivot highs (simplified: just use candle highs)
        recent_highs = df['h'].iloc[-4:].values
        
        # Check for Lower High pattern
        # We want: high[-2] < high[-3] (the previous swing high was lower)
        if len(recent_highs) >= 3:
            last_high = recent_highs[-2]  # Most recent completed candle
            prev_high = recent_highs[-3]  # One before that
            
            if last_high < prev_high:
                return True, f"15m Lower High confirmed ({last_high:.2f} < {prev_high:.2f})"
            else:
                return False, f"15m still making Higher Highs ({last_high:.2f} >= {prev_high:.2f})"
        
        return True, "Could not determine HTF structure"
    
    def count_consecutive_bullish(self, symbol, lookback=10):
        """
        Count consecutive bullish candles before current position.
        Need at least 5 bullish candles before a valid reversal.
        
        Based on Volman: "A reversal after 3 candles is continuation, 
        after 5+ is exhaustion."
        
        Returns:
            tuple: (count, is_sufficient)
        """
        df = self._get_htf_history(symbol, interval="5")  # Use 5m for this
        
        if df is None or len(df) < lookback:
            return 0, True  # Allow if can't determine
        
        count = 0
        # Start from the candle before current (iloc[-2])
        for i in range(-2, -lookback - 2, -1):
            try:
                candle = df.iloc[i]
                is_bullish = candle['c'] > candle['o']
                
                if is_bullish:
                    count += 1
                else:
                    break  # Stop counting on first bearish
            except:
                break
        
        is_sufficient = count >= 5
        return count, is_sufficient
    
    def check_trend_exhaustion(self, symbol):
        """
        Combined check for short entry:
        1. 15m showing weakness (lower high)
        2. 5+ consecutive bullish candles (exhaustion run)
        
        Returns:
            tuple: (is_exhausted, message)
        """
        # Check 1: 15m structure
        has_weakness, weakness_msg = self.check_15m_structure(symbol)
        
        # Check 2: Consecutive bullish count
        bullish_count, is_sufficient = self.count_consecutive_bullish(symbol)
        
        # For shorts, we want:
        # - Either 15m weakness OR significant bullish run
        # - We're being lenient here: one of the conditions is enough
        
        if has_weakness:
            return True, f"HTF Confirmed: {weakness_msg}"
        
        if is_sufficient:
            return True, f"Exhaustion Run: {bullish_count} consecutive bullish candles"
        
        # Neither condition met
        return False, f"No HTF weakness (15m: {weakness_msg}, Bullish run: {bullish_count})"
    
    def get_key_levels(self, symbol):
        """
        Calculate key support/resistance levels from daily chart.
        
        Returns:
            dict: Dictionary of level names and prices
        """
        # Fetch 2 days of data for PDH/PDL
        today = datetime.date.today()
        yesterday = today - datetime.timedelta(days=3)  # Buffer for weekends
        
        data = {
            "symbol": symbol,
            "resolution": "D",
            "date_format": "1",
            "range_from": yesterday.strftime("%Y-%m-%d"),
            "range_to": today.strftime("%Y-%m-%d"),
            "cont_flag": "1"
        }
        
        try:
            response = self.fyers.history(data=data)
            if response.get('s') == 'ok' and response.get('candles') and len(response['candles']) >= 2:
                candles = response['candles']
                
                # Previous day's candle (index -2, today is -1)
                prev_day = candles[-2]
                
                levels = {
                    'PDH': prev_day[2],  # Previous Day High
                    'PDL': prev_day[3],  # Previous Day Low
                    'PDC': prev_day[4],  # Previous Day Close
                }
                
                # Weekly high from available data
                weekly_high = max(c[2] for c in candles)
                weekly_low = min(c[3] for c in candles)
                
                levels['PWH'] = weekly_high
                levels['PWL'] = weekly_low
                
                return levels
        except Exception as e:
            logger.error(f"Failed to fetch daily data for {symbol}: {e}")
        
        return {}
    
    def is_at_key_level(self, symbol, current_price, tolerance_pct=0.5):
        """
        Check if current price is near a key level.
        
        Args:
            symbol: Stock symbol
            current_price: Current LTP
            tolerance_pct: How close (in %) to consider "at level"
            
        Returns:
            tuple: (is_at_level, level_name, level_price)
        """
        levels = self.get_key_levels(symbol)
        
        if not levels:
            return False, None, None
        
        for name, level in levels.items():
            if level == 0:
                continue
                
            distance_pct = abs(current_price - level) / level * 100
            
            if distance_pct <= tolerance_pct:
                return True, name, level
        
        return False, None, None
