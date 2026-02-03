
import sys
import os
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fyers_connect import FyersConnect
from scanner import FyersScanner
import time
import logging

# Setup Logging to Console
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_quality():
    try:
        fyers = FyersConnect().authenticate()
        scanner = FyersScanner(fyers)
        
        # Test Symbols
        symbols = ["NSE:SANSERA-EQ", "NSE:SKIPPER-EQ", "NSE:SBIN-EQ", "NSE:RELIANCE-EQ"]
        
        print("\n--- DEBUGGING CHART QUALITY (09:XX AM Context) ---")
        
        for sym in symbols:
            print(f"\nChecking {sym}...")
            
            # Manual Re-implementation of the logic to print details
            to_date = int(time.time())
            from_date = to_date - (60 * 60) # Last 1 Hour
             
            data = {
                "symbol": sym,
                "resolution": "1",
                "date_format": "0",
                "range_from": str(from_date),
                "range_to": str(to_date),
                "cont_flag": "1"
            }
            
            resp = fyers.history(data=data)
            
            if 'candles' in resp:
                candles = resp['candles']
                count = len(candles)
                print(f"Candles Fetched: {count}")
                
                if count > 0:
                    print(f"First Candle: {candles[0]}")
                    print(f"Last Candle:  {candles[-1]}")
                    
                    zero_vol = 0
                    flat = 0
                    for c in candles:
                        o, h, l, c_p, v = c[1], c[2], c[3], c[4], c[5]
                        if v == 0: zero_vol += 1
                        if o == c_p and h == l: flat += 1
                        
                    ratio = (zero_vol + flat) / count
                    print(f"Zero Vol: {zero_vol} | Flat: {flat} | Ratio: {ratio:.2f}")
                    
                    # Logic Check
                    import datetime
                    now_dt = datetime.datetime.fromtimestamp(to_date)
                    is_early = now_dt.hour < 10
                    min_c = 5 if is_early else 30
                    
                    if count < min_c:
                        print(f"❌ REJECT: Not enough data ({count} < {min_c})")
                    elif ratio > 0.3:
                         print(f"❌ REJECT: Bad Structure (Ratio {ratio:.2f} > 0.3)")
                    else:
                        print("✅ ACCEPT")
                else:
                    print("❌ REJECT: 0 Candles returned")
            else:
                print(f"❌ ERROR: No 'candles' in response: {resp}")
                
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    debug_quality()
