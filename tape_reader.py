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
