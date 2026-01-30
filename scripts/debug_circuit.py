import yfinance as yf
from datetime import datetime

def check_refex_circuit():
    ticker = "REFEX.NS"
    # We can infer circuit from price action if we can't get historical metadata
    # Usually specific bands are 5%, 10%, 20%.
    
    # 1. Get Jan 19 Data
    df = yf.download(ticker, start="2026-01-19", end="2026-01-20", progress=False)
    
    if df.empty:
        print("No data.")
        return

    # Handle MultiIndex
    try:
        # Access scalar values properly for yfinance > 0.2
        open_p = df['Open'].iloc[0].item()
        high_p = df['High'].iloc[0].item()
        close_p = df['Close'].iloc[0].item()
        prev_close_fake = open_p # Approximation if we don't fetch prev day
    except:
        open_p = df['Open'].iloc[0]
        high_p = df['High'].iloc[0]
        close_p = df['Close'].iloc[0]

    # Let's get Prev Close for accurate calculation
    hist = yf.download(ticker, period="5d", progress=False)
    # Get index location of 2026-01-19
    try:
        # Convert index to date strings for finding
        idx_dates = [d.strftime('%Y-%m-%d') for d in hist.index]
        loc_19 = idx_dates.index('2026-01-19')
        prev_close = hist['Close'].iloc[loc_19-1].item()
    except Exception as e:
        print(f"Could not calculate exact prev close: {e}")
        prev_close = open_p # Fallback
        
    print(f"Refex Data (Jan 19):")
    print(f"Prev Close: {prev_close:.2f}")
    print(f"High: {high_p:.2f}")
    
    # Circuit Calculation
    # Bands are calculated on Prev Close.
    limit_5 = prev_close * 1.05
    limit_10 = prev_close * 1.10
    limit_20 = prev_close * 1.20
    
    print(f"\nCircuit Levels (Theoretical):")
    print(f"5%  Limit: {limit_5:.2f}")
    print(f"10% Limit: {limit_10:.2f}")
    print(f"20% Limit: {limit_20:.2f}")
    
    # Compare High to Limits
    print(f"\nAnalysis:")
    if high_p >= limit_10 and high_p < limit_20:
         # If it crossed 10% easily, it's 20%.
         # If it stopped EXACTLY at 10%, it's 10%.
         pass
         
    diff_5 = abs(high_p - limit_5)
    diff_10 = abs(high_p - limit_10)
    diff_20 = abs(high_p - limit_20)
    
    # Check "High" gain %
    gain_pct = ((high_p - prev_close) / prev_close) * 100
    print(f"Actual Day High Gain: {gain_pct:.2f}%")
    
    if gain_pct > 10.1:
        print("Verdict: Circuit is definitely 20% (It crossed 10%).")
    elif gain_pct > 5.1:
         print("Verdict: Likely 10% or 20%.")
         if abs(gain_pct - 10.0) < 0.1:
             print("It hit exactly 10%, suggesting a 10% Limit.")
         else:
             print("It traded freely above 5% and below 10%. likely 20% or 10%.")
    else:
        print("Verdict: Could be 5%.")

if __name__ == "__main__":
    check_refex_circuit()
