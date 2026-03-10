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

    def _find_swing_highs(self, df: pd.DataFrame, window: int = 10) -> list:
        """
        Returns list of (index, price) for confirmed pivot swing highs.
        A pivot high at index i requires: df['h'][i] > df['h'][i-1] AND df['h'][i] > df['h'][i+1].
        Only looks at last 'window' candles to avoid stale pivots.
        Murphy: 'A Lower High is a swing high that terminated below the prior swing high.'
        """
        highs = []
        series = df['h'].iloc[-window:].reset_index(drop=True)
        for i in range(1, len(series) - 1):
            if series.iloc[i] > series.iloc[i - 1] and series.iloc[i] > series.iloc[i + 1]:
                highs.append((i, float(series.iloc[i])))
        return highs
    
    def check_trend_exhaustion(self, symbol, df_15m=None):
        """
        Combined check for short entry (Phase 51 Hardened):
        1. 15m showing weakness (lower high)
        2. 5+ consecutive bullish candles (exhaustion run)
        3. G9.3: HTF Flatness (New for Phase 51)
        
        Returns:
            tuple: (is_exhausted, message)
        """
        import config
        
        # ── Step 0: Fetch/Use 15m data ────────────────────────────
        df = df_15m if df_15m is not None else self._get_htf_history(symbol, interval="15")
        
        if df is None or len(df) < 5:
            return True, "Insufficient HTF for G9 — PASS (Fail-Open)"

        # ── Step 1: 15m Pivot Structure ───────────────────────────
        import config as _cfg
        use_pivot = getattr(_cfg, 'P55_G9_USE_PIVOT_HIGH_DETECTION', True)

        if use_pivot:
            swing_highs = self._find_swing_highs(df, window=10)
            if len(swing_highs) >= 2:
                last_pivot = swing_highs[-1][1]
                prev_pivot = swing_highs[-2][1]
                has_weakness = last_pivot < prev_pivot
                weakness_msg = (
                    f"15m Pivot LH ({last_pivot:.2f} < {prev_pivot:.2f})"
                    if has_weakness else
                    f"15m Pivot HH ({last_pivot:.2f} >= {prev_pivot:.2f})"
                )
            else:
                # Fewer than 2 confirmed swing pivots in last 10 candles → insufficient structure
                has_weakness = False
                weakness_msg = "Insufficient pivot history (< 2 pivots in 10 bars)"
        else:
            # Legacy raw candle comparison (backward compat — flag P55_G9_USE_PIVOT_HIGH_DETECTION=False)
            recent_highs = df['h'].iloc[-3:].values
            last_high    = recent_highs[-1]
            prev_high    = recent_highs[-2]
            has_weakness = last_high < prev_high
            weakness_msg = f"15m LH ({last_high:.2f} < {prev_high:.2f})" if has_weakness else "15m HH/Equal"

        # ── Step 2: Consecutive 5m Bullish Count ──────────────────
        bullish_count, is_sufficient = self.count_consecutive_bullish(symbol)

        # ── Step 3: G9.3 HTF Flatness/Acceleration [Phase 51] ─────
        # Req abs(curr_15m_gain - prev_15m_gain) < 1.0%
        # Rejects if gain accelerated (diff > 2.0%)
        if config.PHASE_51_ENABLED:
            try:
                # Need prev_close to calculate gain % correctly
                # Simplified: compare price change in last two 15m candles
                curr_c = df['c'].iloc[-1]
                prev_c = df['c'].iloc[-2]
                pprev_c = df['c'].iloc[-3]
                
                curr_move_pct = abs(curr_c - prev_c) / prev_c * 100
                prev_move_pct = abs(prev_c - pprev_c) / pprev_c * 100
                acceleration = curr_move_pct - prev_move_pct
                
                if acceleration > 2.0:
                    return False, f"G9.3 REJECT: HTF Accel ({acceleration:.1f}% > 2.0%)"
                
                is_flat = abs(acceleration) < 1.0
                if is_flat:
                    return True, f"G9.3 PASS: HTF Flatness ({abs(acceleration):.2f}% < 1.0%)"
            except Exception as e:
                logger.warning(f"G9.3 Flatness check error: {e}")

        # ── Final Synthesis ───────────────────────────────────────
        if has_weakness:
            return True, f"HTF Weakness: {weakness_msg}"
        
        if is_sufficient:
            return True, f"Exhaustion Run: {bullish_count} consecutive bullish"
        
        return False, f"No HTF weakness (15m: {weakness_msg}, Run: {bullish_count})"
    
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
