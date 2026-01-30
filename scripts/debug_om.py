import yfinance as yf
import pandas as pd

def check_om_infra():
    ticker = "OMINFRAL.NS"
    print(f"Fetching data for {ticker} (Jan 19, 2026)...")
    
    # Needs Data for 19th and Prev Close (16th)
    data = yf.download(ticker, start="2026-01-15", end="2026-01-20", progress=False)
    
    if data.empty:
        print("No Data Found.")
        return

    try:
        # Prev Close (Jan 16)
        # Using string access for robustness
        try:
             # Ensure index is datetime and localized?
             # YFinance usually returns localized TZ.
             # We will just use position if possible or safe string lookup
             # Let's dump index to see format
             pass
        except:
             pass
             
        # Simple iloc approach since we know the range is small
        # Row -1 is last available (likely 19th), Row -2 is 16th.
        
        # Check specific date index
        # data.index is DatetimeIndex
        
        day_19 = data[data.index.strftime('%Y-%m-%d') == '2026-01-19']
        day_16 = data[data.index.strftime('%Y-%m-%d') == '2026-01-16']
        
        if day_19.empty:
            print("Jan 19 Data Missing.")
            return
            
        # Get Scalars
        try:
            close_19 = day_19['Close'].values[0]
            high_19 = day_19['High'].values[0]
            vol_19 = day_19['Volume'].values[0]
            
            close_16 = day_16['Close'].values[0] if not day_16.empty else 0
        except:
             # MultiIndex fallback
             close_19 = day_19['Close'][ticker].values[0]
             high_19 = day_19['High'][ticker].values[0]
             vol_19 = day_19['Volume'][ticker].values[0]
             close_16 = day_16['Close'][ticker].values[0] if not day_16.empty else 0
             
        if close_16 == 0:
             # Fallback
             close_16 = day_19['Open'].values[0]
             
        # Extract scalar native types
        close_16 = float(close_16)
        high_19 = float(high_19)
        close_19 = float(close_19)
        vol_19 = int(vol_19)
             
        gain_pct = ((high_19 - close_16) / close_16) * 100
        end_pct = ((close_19 - close_16) / close_16) * 100
        
        print(f"\nAnalysis for {ticker}:")
        print(f"Prev Close: {close_16:.2f}")
        print(f"High: {high_19:.2f} (Max Gain: {gain_pct:.2f}%)")
        print(f"Close: {close_19:.2f} (End Gain: {end_pct:.2f}%)")
        print(f"Volume: {vol_19}")
        
        # Evaluation
        is_circuit_trap = (high_19 == close_19) and (gain_pct > 4.5)
        
        print("\nBot Criteria Check:")
        print(f"1. Volume > 100k: {'PASS' if vol_19 > 100000 else 'FAIL ({vol_19})'}")
        print(f"2. Max Gain > 5%: {'PASS' if gain_pct >= 5.0 else 'FAIL'}")
        print(f"3. Circuit Trap Risk: {'HIGH' if is_circuit_trap else 'LOW'}")
        
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_om_infra()
