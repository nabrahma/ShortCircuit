import pandas as pd
import numpy as np
from fyers_connect import FyersConnect
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("MarketProfiler")

class MarketProfiler:
    def __init__(self, fyers_instance=None):
        self.fyers = fyers_instance if fyers_instance else FyersConnect().authenticate()

    def calculate_volume_profile(self, df_m1):
        """
        Approximates Volume Profile from 1-minute OHLCV data.
        Returns: POC (Point of Control), VA (Value Area) High/Low.
        """
        if df_m1.empty:
            return None
            
        # 1. Create Price Range Buckets (Bin Size = 0.05% of price or Fixed 1 Rupee?)
        # Let's use dynamic binning based on volatility or price range.
        min_price = df_m1['low'].min()
        max_price = df_m1['high'].max()
        
        # Bin size: approx 0.1% or tick size. EIMCO ~1800 -> 1.8. Let's use 1.0
        bin_size = max(0.05, (max_price - min_price) / 100) # 100 bins
        
        bins = np.arange(min_price, max_price + bin_size, bin_size)
        volume_profile = pd.Series(0.0, index=bins[:-1])
        
        # 2. Distribute Volume
        # Method: For each candle, distribute volume evenly across its High-Low range.
        for _, row in df_m1.iterrows():
            l, h, v = row['low'], row['high'], row['volume']
            if h == l:
                # Flat candle (rare, or doji)
                target_bin = bins[bins <= l][-1]
                if target_bin in volume_profile:
                       volume_profile[target_bin] += v
                continue
                
            # Find bins covered by this candle
            touched_bins = volume_profile.index[(volume_profile.index >= l) & (volume_profile.index <= h)]
            
            if len(touched_bins) > 0:
                vol_per_bin = v / len(touched_bins)
                volume_profile[touched_bins] += vol_per_bin
                
        # 3. Find Context
        poc_price = volume_profile.idxmax()
        total_volume = volume_profile.sum()
        
        # Value Area (70% of volume)
        sorted_indices = volume_profile.sort_values(ascending=False).index
        cum_vol = 0
        va_bins = []
        for p in sorted_indices:
            cum_vol += volume_profile[p]
            va_bins.append(p)
            if cum_vol >= 0.7 * total_volume:
                break
                
        val = min(va_bins) # Value Area Low
        vah = max(va_bins) # Value Area High
        
        return {
            'poc': poc_price,
            'val': val,
            'vah': vah,
            'profile': volume_profile
        }

    def get_market_depth_imbalance(self, symbol):
        """
        Fetches Level 2 Depth (Quote) and calculates Bid/Ask Imbalance.
        """
        data = {"symbols": symbol}
        response = self.fyers.quotes(data=data)
        
        if 'd' in response and len(response['d']) > 0:
            q = response['d'][0]
            # Fyers V3 'v' object
            if 'v' in q:
                # total_buy_qty vs total_sell_qty
                b_qty = q['v'].get('total_buy_qty', 0)
                s_qty = q['v'].get('total_sell_qty', 1) # avoid div0
                
                ratio = b_qty / s_qty
                sentiment = "BULLISH" if ratio > 1.5 else "BEARISH" if ratio < 0.6 else "NEUTRAL"
                
                return {
                    'bid_qty': b_qty,
                    'ask_qty': s_qty,
                    'ratio': ratio,
                    'sentiment': sentiment
                }
        return None

if __name__ == "__main__":
    # Test on EIMCOELECO
    mp = MarketProfiler()
    
    # Fetch Data
    symbol = "NSE:EIMCOELECO-EQ"
    today = "2026-01-08"
    fyers = mp.fyers
    data = {
        "symbol": symbol, "resolution": "1", "date_format": "1", 
        "range_from": today, "range_to": today, "cont_flag": "1"
    }
    resp = fyers.history(data=data)
    df = pd.DataFrame(resp['candles'], columns=['epoch','open','high','low','close','volume'])
    df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    
    # Calculate Profile
    vp = mp.calculate_volume_profile(df)
    
    if vp:
        print(f"Volume Profile for {symbol}:")
        print(f"POC (Point of Control): {vp['poc']:.2f}")
        print(f"VA (Value Area): {vp['val']:.2f} - {vp['vah']:.2f}")
        
    # Check Depth (Live/Snapshot)
    depth = mp.get_market_depth_imbalance(symbol)
    if depth:
        print(f"Depth Imbalance: Ratio {depth['ratio']:.2f} ({depth['sentiment']})")
