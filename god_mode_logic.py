import pandas as pd
import numpy as np
import logging
# from scipy.stats import linregress

# Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("GodModeLogic")

class GodModeAnalyst:
    def __init__(self):
        pass

    def calculate_vwap_slope(self, df, window=30):
        """
        Calculates the slope of the VWAP curve over the last 'window' candles.
        Returns:
            slope (float): Interpretation: < 0.05 is Flat (Reversion), > 0.1 is Trend.
            status (str): "FLAT" or "TRENDING"
        """
        if df.empty or len(df) < window:
            return 0.0, "INSUFFICIENT_DATA"
            
        # Get last N VWAP values
        if 'vwap' not in df.columns:
            # Approx VWAP if not present (simple calculation)
            v = df['volume'].values
            tp = (df['high'] + df['low'] + df['close']) / 3
            df['vwap'] = (tp * v).cumsum() / v.cumsum()
            
        y = df['vwap'].iloc[-window:].values
        x = np.arange(len(y))
        
        if len(y) < 2: return 0.0, "INSUFFICIENT_DATA"
        
        # Linear Regression using Numpy (Degree 1)
        slope, intercept = np.polyfit(x, y, 1)
        
        # Normalize slope
        # Better: Slope as % of Price.
        # normalized_slope = slope / df['close'].iloc[-1] * 100
        
        # For simplicity, let's use raw angle or just look at R-squared for linearity?
        # Let's standardize: Change in VWAP per minute / Current Price * 10000 (Basis points)
        
        pct_slope = (slope / df['close'].iloc[-1]) * 10000
        
        status = "FLAT" if abs(pct_slope) < 5 else "TRENDING" # Threshold need tuning
        
        return pct_slope, status

    def detect_structure(self, df):
        """
        Analyzes the last candle for Absorption or Exhaustion.
        """
        if df.empty: return None
        
        last = df.iloc[-1]
        
        # Volatility stats for Z-Score
        recent_vol = df['volume'].iloc[-20:] # Last 20 candles
        avg_vol = recent_vol.mean()
        std_vol = recent_vol.std()
        
        current_vol = last['volume']
        z_score_vol = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0
        
        # Candle shape
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - max(last['open'], last['close'])
        
        structure = "NORMAL"
        
        # 1. Absorption: High Vol + Tiny Body (Effort vs Result divergence)
        if z_score_vol > 2.0 and body < (last['close'] * 0.0005): # 0.05% body
            structure = "ABSORPTION"
            
        # 2. Exhaustion: High Vol + Long Wick (Shooting Star)
        elif z_score_vol > 1.5 and upper_wick > (2 * body):
            structure = "EXHAUSTION"
            
        return structure, z_score_vol

    def check_constraints(self, ltp, day_high, trend_gain):
        """
        The "Ethos" Check.
        Phase 13: Pullback Scalper Rules.
        """
        if trend_gain < 7.0:
            return False, f"Gain {trend_gain:.1f}% too low (< 7%)"
            
        if trend_gain > 15.0:
            return False, f"Gain {trend_gain:.1f}% too high (> 15%) - Circuit Risk"
            
        # 2. Smart Day High Proximity
        # Base: Allow 3.0% pullback (Discretionary vs Mechanical).
        # Turbo: If Gain > 10%, allow 5.0% pullback.
        
        dist_from_high_pct = (day_high - ltp) / day_high * 100
        
        allowed_dist = 3.0 
        if trend_gain > 10.0:
            allowed_dist = 5.0
            
        if dist_from_high_pct > allowed_dist:
             return False, f"Too far from High ({dist_from_high_pct:.2f}%) > {allowed_dist}%"
            
        return True, "PASSED"

    # Phase 19: ATR
    def calculate_atr(self, df, period=14):
        """
        Calculates Average True Range (ATR).
        """
        try:
            high = df['high']
            low = df['low']
            close = df['close']
            
            # TR1 = High - Low
            # TR2 = abs(High - PrevClose)
            # TR3 = abs(Low - PrevClose)
            
            prev_close = close.shift(1)
            tr1 = high - low
            tr2 = (high - prev_close).abs()
            tr3 = (low - prev_close).abs()
            
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
            atr = tr.rolling(window=period).mean()
            
            return atr.iloc[-1]
            
        except Exception as e:
            # logger.error(f"ATR Calc Error: {e}")
            return 1.0 # Fallback default

    # Phase 21: Advanced Reversal Patterns
    def detect_structure_advanced(self, df):
        """
        Expanded Reversal Logic:
        1. Single Candle (Shooting Star, Doji)
        2. Multi-Candle (Bearish Engulfing, Evening Star)
        """
        if df.empty or len(df) < 3: return "NORMAL", 0
        
        # Last 3 candles
        c1 = df.iloc[-3] # 2 candles ago
        c2 = df.iloc[-2] # Prev candle
        c3 = df.iloc[-1] # Current candle (Closed)
        
        # Helper: Get Body/Range
        def get_candle_stats(row):
            body = abs(row['close'] - row['open'])
            direction = 1 if row['close'] > row['open'] else -1 # 1 Green, -1 Red
            upper_wick = row['high'] - max(row['open'], row['close'])
            total_range = row['high'] - row['low']
            if total_range == 0: total_range = 0.05
            return body, direction, upper_wick, total_range
            
        b1, d1, uw1, r1 = get_candle_stats(c1)
        b2, d2, uw2, r2 = get_candle_stats(c2)
        b3, d3, uw3, r3 = get_candle_stats(c3)
        
        # Vol Stats for Z-Score (on C3)
        recent_vol = df['volume'].iloc[-20:-1]
        avg_vol = recent_vol.mean()
        std_vol = recent_vol.std()
        current_vol = c3['volume']
        z_score = (current_vol - avg_vol) / std_vol if std_vol > 0 else 0
        
        # Pattern 1: Bearish Engulfing (ZENTEC Killer)
        # Prev Green, Curr Red, Curr Body > Prev Body, Curr Open > Prev Close
        if d2 == 1 and d3 == -1:
            if b3 > b2 and c3['close'] < c2['open']:
                # Filter: Significant size (not tiny candles)
                 return "BEARISH_ENGULFING", z_score
                 
        # Pattern 2: Evening Star (Green -> Doji -> Red)
        # C1 Green, C2 Small Body (Gap Up ideally), C3 Red (closes deep into C1)
        if d1 == 1 and b2 < (r2 * 0.3) and d3 == -1:
             if c3['close'] < (c1['open'] + c1['close'])/2: # Closes below midpoint of C1
                 return "EVENING_STAR", z_score

        # Pattern 3: Shooting Star (Legacy)
        # High Vol + Long Wick
        if uw3 > (2 * b3) and z_score > 1.5:
            return "SHOOTING_STAR", z_score
            
        # Pattern 4: Doji / Absorption
        if z_score > 2.0 and b3 < (c3['close'] * 0.0005):
            return "ABSORPTION_DOJI", z_score
            
        return "NORMAL", z_score

    # Phase 21: Statistical Extremes
    def calculate_vwap_bands(self, df):
        """
        Returns dist from VWAP in Standard Deviations.
        """
        if 'vwap' not in df.columns: return 0
        
        # Std Dev of Price relative to VWAP over last 20 candles
        # Approx: StdDev of (Close - VWAP)
        
        window = df.iloc[-20:]
        diffs = window['close'] - window['vwap']
        std_dev = diffs.std()
        
        if std_dev == 0: return 0
        
        current_diff = df.iloc[-1]['close'] - df.iloc[-1]['vwap']
        score = current_diff / std_dev
        
        return score # > 2.0 is +2SD
        
    # Phase 21: Momentum
    def calculate_rsi(self, df, period=14):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
        
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi.iloc[-1]
        
    def check_rsi_divergence(self, df):
        # Higher High in Price, Lower High in RSI (Last 10 frames)
        # Simplified: Price Slope Positive, RSI Slope Negative
        try:
           p_slope, _ = self.calculate_vwap_slope(df[['close', 'volume']], window=10) # Reuse slope logic on Price? No need generic.
           
           # Quick Linear Reg on last 10 RSI points
           curr_rsi = self.calculate_rsi(df) # Logic needs full series.
           # Recalc series
           delta = df['close'].diff()
           gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
           loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
           rs = gain/loss
           rsi_series = 100 - (100/(1+rs))
           
           recent_rsi = rsi_series.iloc[-10:]
           recent_price = df['close'].iloc[-10:]
           
           # Check Price trend
           p_start, p_end = recent_price.iloc[0], recent_price.iloc[-1]
           r_start, r_end = recent_rsi.iloc[0], recent_rsi.iloc[-1]
           
           if p_end > p_start and r_end < r_start:
               return True
           return False
        except:
            return False

    # Phase 21: AMT
    def calculate_market_profile(self, df):
        """
        Approx VAH/VAL using Volume Profile on DataFrame.
        Returns VAH, VAL, POC.
        """
        # Create Price Bins
        price_min = df['low'].min()
        price_max = df['high'].max()
        tick_size = 0.05
        bins = np.arange(price_min, price_max + tick_size, tick_size)
        
        # Bin Volume
        # Simple attribution: Total candle vol to Close price bin (approx)
        # Better: TPO? No, use close.
        vol_dist = df.groupby(pd.cut(df['close'], bins))['volume'].sum()
        
        total_vol = vol_dist.sum()
        sorted_dist = vol_dist.sort_values(ascending=False)
        
        # POC
        poc_bin = sorted_dist.index[0]
        poc = poc_bin.mid
        
        # VA (70%)
        target_vol = total_vol * 0.7
        current_vol = 0
        va_indices = []
        
        # Grow from POC out? 
        # Simple for now: Just take top 70% volume bins (Aggregated Profile, not ordered)
        # Correct way involves growing up/down from POC. 
        # For simplicity in this bot: Just return POC.
        # VAH/VAL requires ordered traversal.
        
        return poc

    # Phase 22: Fibonacci Golden Ratio
    def calculate_fib_levels(self, df):
        """
        Identifies recent Swing High/Low and calculates Retracements (.382, .5, .618).
        Logic:
        1. Find Highest High (HH) and Lowest Low (LL) in last 50 candles.
        2. Determine Context: Are we closer to Low (Downtrend) or High (Uptrend)?
           Actually, better logic: Find the massive Impulse.
           Simplified: Take Range of last 100 candles.
        Returns: Dict of Levels.
        """
        if len(df) < 50: return {}
        
        # Lookback 50 candles
        window = df.iloc[-50:]
        
        high = window['high'].max()
        low = window['low'].min()
        
        # Time of High vs Time of Low
        high_idx = window['high'].idxmax()
        low_idx = window['low'].idxmin()
        
        levels = {}
        
        # Scenario A: Downtrend (High -> Low)
        # We are looking for Retracement UP (Bear Flag)
        if high_idx < low_idx:
            direction = "DOWN"
            diff = high - low
            # Valid Retracement Levels (Price moving up from Low)
            levels['fib_382'] = low + (diff * 0.382)
            levels['fib_5']   = low + (diff * 0.5)
            levels['fib_618'] = low + (diff * 0.618)
            levels['trend'] = "DOWN"
            
        # Scenario B: Uptrend (Low -> High)
        # We are looking for Retracement DOWN (Bull Flag)
        else:
            direction = "UP"
            diff = high - low
            # Valid Retracement Levels (Price moving down from High)
            levels['fib_382'] = high - (diff * 0.382)
            levels['fib_5']   = high - (diff * 0.5)
            levels['fib_618'] = high - (diff * 0.618)
            levels['trend'] = "UP"
            
        return levels

if __name__ == "__main__":
    # Test
    gm = GodModeAnalyst()
    
    # Mock Data
    data = {
        'close': [100, 100.1, 100.2, 100.1, 100.05],
        'volume': [1000, 1200, 1100, 5000, 1000], # Spike at end
        'open': [100, 100.1, 100.2, 100.3, 100.04],
        'high': [100.1, 100.2, 100.3, 100.5, 100.05], # Long wick at end
        'low': [99.9, 100.1, 100.1, 100.1, 100.00]
    }
    df = pd.DataFrame(data)
    
    # Calc Slope
    slope, status = gm.calculate_vwap_slope(df, window=5)
    print(f"Slope: {slope:.2f} ({status})")
    
    # Calc Structure (Last candle 4: High vol 1000? No 1000 is small vs 5000)
    # Let's test the massive vol candle (index 3)
    df_slice = df.iloc[:4]
    struct, z = gm.detect_structure(df_slice)
    print(f"Structure (Candle 3): {struct} (Z-Vol: {z:.2f})")
    
    # Check Constraints
    ok, msg = gm.check_constraints(ltp=100.1, day_high=102, trend_gain=9.0)
    print(f"Constraints: {ok} -> {msg}")
