import yfinance as yf
from datetime import datetime
import pandas as pd

def check_refex():
    # Fetch data for Refex and a benchmark (like Nifty) to ensure trading day validity
    # We need Jan 19 2026.
    # To get % change, we need Prev Close (Jan 16 2026).
    
    ticker = "REFEX.NS"
    print(f"Fetching data for {ticker}...")
    
    # Fetch a range covering the date
    df = yf.download(ticker, start="2026-01-15", end="2026-01-21", progress=False)
    
    if df.empty:
        print("No data found for REFEX.NS")
        return

    print("Data fetched:")
    print(df)

    # Specific Dates
    # YFinance index is Datetime.
    try:
        # Adjust for Timezone if needed or use string slicing
        # Jan 19 is a Monday. Jan 16 is Friday.
        
        # Accessing via string might find the row
        try:
            day_data = df.loc['2026-01-19']
            prev_day_data = df.loc['2026-01-16'] # Assuming Friday was trading
        except KeyError:
             print("Could not find exact dates in index. Available dates:")
             print(df.index)
             return

        # Extract values (Handling MultiIndex columns if present in new yfinance)
        # YFinance v0.2+ returns MultiIndex (Price, Ticker)
        
        if isinstance(df.columns, pd.MultiIndex):
            close_19 = day_data['Close'][ticker]
            high_19 = day_data['High'][ticker]
            vol_19 = day_data['Volume'][ticker]
            close_16 = prev_day_data['Close'][ticker]
        else:
            close_19 = day_data['Close']
            high_19 = day_data['High']
            vol_19 = day_data['Volume']
            close_16 = prev_day_data['Close']
            
        # 1. Gain Check
        # Scanner checks "Day Change %" which is (LTP - PrevClose) / PrevClose
        # We'll use Close of 19th as proxy for LTP, or High if we want to see max potential.
        # But scanner runs live, so let's look at the Close to see EOD status, 
        # and High to see if it *ever* hit the condition.
        
        pct_change_close = ((close_19 - close_16) / close_16) * 100
        pct_change_high = ((high_19 - close_16) / close_16) * 100
        
        print(f"\nAnalysis for {ticker} on Jan 19, 2026:")
        print(f"Prev Close (Jan 16): {close_16:.2f}")
        print(f"High (Jan 19): {high_19:.2f} (Max Gain: {pct_change_high:.2f}%)")
        print(f"Close (Jan 19): {close_19:.2f} (End Gain: {pct_change_close:.2f}%)")
        print(f"Volume: {vol_19}")
        
        # Evaluation against criteria
        # Criteria 1: Volume > 100,000
        pass_vol = vol_19 > 100000
        print(f"Criteria [Volume > 100k]: {'PASS' if pass_vol else 'FAIL'}")
        
        # Criteria 2: Gain > 5% (MoneyControl/Scanner.py) or 10%-18% (MarketScanner.py)
        # Did it *reach* these levels?
        pass_gain_5 = pct_change_high >= 5.0
        pass_gain_10 = pct_change_high >= 10.0
        
        print(f"Criteria [Gain >= 5%]: {'PASS' if pass_gain_5 else 'FAIL'}")
        print(f"Criteria [Gain >= 10%]: {'PASS' if pass_gain_10 else 'FAIL'}")
        
        # Criteria 3: Upper Circuit Trap
        # Assuming 20% circuit for Refex? Or 5%?
        # If it hit 5% and stopped, it might be a circuit.
        # Check if High == Close (often indicates UC lock)
        is_circuit_locked = (high_19 == close_19) and (pass_gain_5 or pass_gain_10)
        if is_circuit_locked:
            print("Note: High == Close. Possible Upper Circuit Lock.")
            
        turnover = close_19 * vol_19
        print(f"Turnover: {turnover:,.2f} (Need > 5Cr)")
        pass_turnover = turnover > 50000000
        print(f"Criteria [Turnover > 5Cr]: {'PASS' if pass_turnover else 'FAIL'}")

    except Exception as e:
        print(f"Error calculating: {e}")

if __name__ == "__main__":
    check_refex()
