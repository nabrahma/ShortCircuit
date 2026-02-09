import pandas as pd
import numpy as np
import logging

logger = logging.getLogger("TapeReader")

class TapeReader:
    def __init__(self):
        pass

    def detect_absorption(self, df, lookback=3):
        """
        Detects 'Absorption': High Volume but Price fails to advance.
        Sign of a hidden limit seller (Wall) absorbing demand.
        """
        if df is None or len(df) < lookback + 1:
            return False, "Insufficient Data"

        # Analyze last few candles (excluding current forming one if mostly incomplete)
        # Assuming df includes completed candles mostly.
        recent = df.iloc[-lookback:]
        
        # Logic:
        # 1. Volume is High (Relatively)
        # 2. Price Change is Low (Stall)
        # 3. Wicks at top (Rejection)
        
        avg_vol = df['volume'].iloc[:-lookback].mean()
        if avg_vol == 0: avg_vol = 1
        
        recent_vol_avg = recent['volume'].mean()
        rvol = recent_vol_avg / avg_vol
        
        # Calculate 'Efficiency': Price Move / Volume
        # Low Efficiency + High Volume = Absorption
        total_vol = recent['volume'].sum()
        price_change = abs(recent['close'].iloc[-1] - recent['open'].iloc[0])
        
        efficiency = price_change / total_vol if total_vol > 0 else 0
        
        # Thresholds (Tunable)
        is_high_vol = rvol > 1.5
        is_stalled = efficiency < 0.0001 # Very little move per unit of volume
        
        # Check for Upper Wicks (Selling pressure)
        upper_wicks = recent['high'] - recent[['open', 'close']].max(axis=1)
        body_sizes = abs(recent['close'] - recent['open'])
        
        avg_wick = upper_wicks.mean()
        avg_body = body_sizes.mean()
        
        is_wicky = avg_wick > avg_body * 0.5
        
        if is_high_vol and (is_stalled or is_wicky):
            return True, f"Absorption Detected (RVOL: {rvol:.1f}, Eff: {efficiency:.6f})"
            
        return False, None

    def detect_stall(self, df):
        """
        Detects 'Drift' or 'Time Correction' at Highs.
        Price stops making new highs but doesn't crash yet.
        (The 'Om Infra' Logic)
        """
        if len(df) < 5: return False, None
        
        last_5 = df.iloc[-5:]
        
        # Check if we are near Day High
        day_high = df['high'].max()
        curr_price = last_5['close'].iloc[-1]
        
        if curr_price < day_high * 0.98:
            return False, None # Not at highs
            
        # Check Slope/Momentum
        # If max high of last 5 is same as last 1 (flat top)
        # and candles are small.
        
        highs = last_5['high'].values
        # If standard deviation of highs is very low, it's flat.
        flatness = np.std(highs)
        
        is_flat = flatness < (curr_price * 0.001) # 0.1% variance
        
        if is_flat:
            return True, "Price Stalled at Highs (Drift)"
            
        return False, None

    def analyze_depth(self, depth_data):
        """
        Analyzes Level 2 (Total Bid/Ask) for Imbalances.
        """
        try:
            # Fyers depth format usually:
            # {'totalbuyqty': 100, 'totalsellqty': 500, 'bids': [], 'asks': []}
            
            total_buy = depth_data.get('totalbuyqty', 0)
            total_sell = depth_data.get('totalsellqty', 0)
            
            if total_buy == 0 or total_sell == 0:
                return 0, "No Depth"
                
            ratio = total_sell / total_buy
            
            # Ratio > 1.0 means More Sellers (Bearish Pressure?)
            # CAUTION: In strong trends, Walls are pulled. 
            # But "Glued to Orderbook" usually means watching the Wall hold.
            
            if ratio > 2.5:
                # 2.5x more sellers than buyers limits
                return ratio, f"Bearish Wall (Sell/Buy: {ratio:.1f})"
            elif ratio < 0.4:
                return ratio, f"Bullish Support (Buy/Sell: {1/ratio:.1f})"
                
            return ratio, "Balanced"
            
        except Exception as e:
            logger.error(f"Depth Error: {e}")
            return 0, "Error"

    # ===== ORDERFLOW PRINCIPLES =====

    def check_round_number(self, ltp):
        """
        Orderflow: Round numbers attract liquidity for reversals.
        Checks if price is near a psychological level (100, 500, 1000, etc.)
        """
        if ltp <= 0:
            return False, None
            
        # Define round number intervals based on price range
        if ltp < 100:
            interval = 10  # 10, 20, 30...
        elif ltp < 500:
            interval = 50  # 50, 100, 150...
        elif ltp < 1000:
            interval = 100  # 100, 200, 300...
        elif ltp < 5000:
            interval = 500  # 500, 1000, 1500...
        else:
            interval = 1000  # 1000, 2000, 3000...
        
        # Find nearest round number
        nearest_round = round(ltp / interval) * interval
        distance_pct = abs(ltp - nearest_round) / ltp * 100
        
        # If within 0.5% of a round number, it's significant
        if distance_pct < 0.5:
            return True, f"Near Round ({nearest_round})"
        
        return False, None

    def detect_large_wick(self, df, lookback=5):
        """
        Orderflow: Large wicks frequently get partially filled.
        Detects if recent candle has large upper wick (rejection) that may get filled.
        """
        if df is None or len(df) < lookback:
            return False, None
            
        last = df.iloc[-1]
        body = abs(last['close'] - last['open'])
        upper_wick = last['high'] - max(last['open'], last['close'])
        lower_wick = min(last['open'], last['close']) - last['low']
        total_range = last['high'] - last['low']
        
        if total_range == 0:
            return False, None
            
        # Large upper wick = rejection at highs (bearish)
        upper_wick_ratio = upper_wick / total_range
        
        # If upper wick is > 60% of total range, it's a strong rejection
        if upper_wick_ratio > 0.6 and upper_wick > body * 2:
            target_fill = last['high'] - (upper_wick * 0.5)  # 50% wick fill target
            return True, f"Large Wick Reject (Fill Target: {target_fill:.2f})"
        
        return False, None

    def detect_bad_high(self, df, depth_data=None):
        """
        Orderflow: Too much selling at a high = bad high.
        Detects if we're at day high with heavy sell pressure (good short zone).
        """
        if df is None or len(df) < 10:
            return False, None
            
        day_high = df['high'].max()
        curr_high = df.iloc[-1]['high']
        curr_close = df.iloc[-1]['close']
        
        # Check if we're at or near day high
        at_day_high = curr_high >= day_high * 0.998
        
        if not at_day_high:
            return False, None
        
        # Check for rejection (close below high)
        rejection = (curr_high - curr_close) / curr_high * 100 > 0.3  # 0.3% rejection
        
        # Check depth for heavy sellers
        heavy_sellers = False
        if depth_data:
            total_buy = depth_data.get('totalbuyqty', 0)
            total_sell = depth_data.get('totalsellqty', 0)
            if total_buy > 0:
                heavy_sellers = (total_sell / total_buy) > 1.5
        
        if rejection and heavy_sellers:
            return True, "Bad High (Heavy Sellers + Rejection)"
        elif rejection:
            return True, "Bad High (Rejection at Day High)"
            
        return False, None

    def detect_bad_low(self, df, depth_data=None):
        """
        Orderflow: Too much buying at a low = bad low.
        Detects if we're at day low with heavy buy pressure (avoid shorting here).
        """
        if df is None or len(df) < 10:
            return False, None
            
        day_low = df['low'].min()
        curr_low = df.iloc[-1]['low']
        curr_close = df.iloc[-1]['close']
        
        # Check if we're at or near day low
        at_day_low = curr_low <= day_low * 1.002
        
        if not at_day_low:
            return False, None
        
        # Check for bounce (close above low)
        bounce = (curr_close - curr_low) / curr_low * 100 > 0.3  # 0.3% bounce
        
        # Check depth for heavy buyers
        heavy_buyers = False
        if depth_data:
            total_buy = depth_data.get('totalbuyqty', 0)
            total_sell = depth_data.get('totalsellqty', 0)
            if total_sell > 0:
                heavy_buyers = (total_buy / total_sell) > 1.5
        
        if bounce and heavy_buyers:
            return True, "Bad Low (Heavy Buyers + Bounce) - AVOID SHORT"
        elif bounce:
            return True, "Bad Low (Bounce at Day Low) - AVOID SHORT"
            
        return False, None

    def detect_trapped_positions(self, df, lookback=10):
        """
        Orderflow: Trapped positions fuel best reversals.
        Detects SFP-like patterns where buyers got trapped at highs.
        """
        if df is None or len(df) < lookback:
            return False, None
            
        recent = df.iloc[-lookback:]
        
        # Find the high point in recent candles
        high_idx = recent['high'].idxmax()
        high_candle = df.loc[high_idx]
        high_price = high_candle['high']
        
        # Current price
        curr_close = df.iloc[-1]['close']
        
        # Check if price has dropped from that high (trapped longs)
        drop_pct = (high_price - curr_close) / high_price * 100
        
        # Check volume at high vs now
        vol_at_high = high_candle['volume']
        avg_vol = recent['volume'].mean()
        
        # High volume at top + drop = trapped buyers
        if drop_pct > 1.0 and vol_at_high > avg_vol * 1.5:
            return True, f"Trapped Longs (Drop: {drop_pct:.1f}%, High Vol at Top)"
        
        return False, None

    def detect_aggression_no_progress(self, df, lookback=5):
        """
        Orderflow: Aggression without progression = absorption.
        High volume but price going nowhere = hidden limit order absorbing.
        """
        if df is None or len(df) < lookback + 5:
            return False, None
            
        recent = df.iloc[-lookback:]
        prior = df.iloc[-(lookback+5):-lookback]
        
        # Calculate volume spike
        recent_vol = recent['volume'].mean()
        prior_vol = prior['volume'].mean()
        
        if prior_vol == 0:
            return False, None
            
        vol_ratio = recent_vol / prior_vol
        
        # Calculate price progression
        price_range = recent['high'].max() - recent['low'].min()
        avg_price = recent['close'].mean()
        range_pct = (price_range / avg_price) * 100
        
        # High volume (>1.5x) but low range (<0.5%) = absorption
        if vol_ratio > 1.5 and range_pct < 0.5:
            return True, f"Absorption (Vol: {vol_ratio:.1f}x, Range: {range_pct:.2f}%)"
        
        return False, None
