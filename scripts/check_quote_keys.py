
import sys
import os
import logging
# Add parent dir
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from fyers_connect import FyersConnect

logging.basicConfig(level=logging.INFO)

def check_keys():
    try:
        fyers = FyersConnect().authenticate()
        
        # Test with a common stock
        symbol = "NSE:SBIN-EQ"
        data = {"symbols": symbol}
        
        print(f"Fetching depth for {symbol}...")
        resp = fyers.depth(data={"symbol": symbol, "ohlcv_flag":"1"})
        
        if 'd' in resp:
            quote = resp['d'].get(symbol, {})
            print("\n--- Depth Data Keys ---")
            for k, v in quote.items():
                print(f"{k}: {v}")
                
            if 'v' in quote:
                print("\n--- 'v' (Values) Object ---")
                for k, v in quote['v'].items():
                    print(f"{k}: {v}")
                    
    except Exception as e:
        print(e)

if __name__ == "__main__":
    check_keys()
