import yfinance as yf
import pandas as pd
from datetime import datetime

def check_real_intraday():
    ticker = "OMINFRAL.NS"
    print(f"Fetching REAL Intraday Data for {ticker} (Jan 19)...")
    
    # Try fetching 5m data (yfinance usually has 60 days of 5m data)
    # Today is Jan 19 2026.
    try:
        df = yf.download(ticker, interval="5m", period="1d", progress=False)
        
        if df.empty:
            print("No real intraday data found via yfinance.")
            return

        print("Data Downloaded:")
        print(df.head())
        print(df.tail())
        
        # Check 10:04 AM (approx)
        # 5m candles: 10:00 -> 10:05.
        
        # Localize if needed (YF is often UTC or Local)
        # Let's pivot to string matching for safety
        
        target_time = "10:00"
        
        found = False
        for idx, row in df.iterrows():
            # Idx is usually datetime
            ts_str = idx.strftime('%H:%M')
            
            if ts_str.startswith("10:0"): # 10:00 to 10:05
                # Extract scalar values
                try:
                    o = row['Open'].item()
                    h = row['High'].item()
                    l = row['Low'].item()
                    c = row['Close'].item()
                    v = row['Volume'].item()
                except:
                    o = row['Open']
                    h = row['High']
                    l = row['Low']
                    c = row['Close']
                    v = row['Volume']
                    
                print(f"Time: {ts_str} | Open: {o:.2f} | High: {h:.2f} | Low: {l:.2f} | Close: {c:.2f} | Vol: {v}")
                found = True
        
        if not found:
            print("Could not find 10:00 AM range.")
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_real_intraday()
