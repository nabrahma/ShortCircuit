import yfinance as yf
import pandas as pd
from datetime import datetime

def analyze_losses():
    # Targets
    # 1. BMWVENTLTD.NS (BMW Industries)
    # 2. STYLEBAAZA.NS (Style Baazar)
    
    tickers = ["BMWVENTLTD.NS", "STYLEBAAZA.NS"]
    
    print("Fetching data for BMWVENTLTD and STYLEBAAZA (Jan 19, 2026)...")
    
    data = yf.download(tickers, start="2026-01-19", end="2026-01-20", group_by='ticker', progress=False)
    
    # Prev Day (Jan 16)
    prev_data = yf.download(tickers, start="2026-01-15", end="2026-01-17", group_by='ticker', progress=False)
    
    with open("analysis_results.txt", "w", encoding="utf-8") as f:
        for sym in tickers:
            f.write(f"\n--- ANALYSIS: {sym} ---\n")
            try:
                # Current Day (Jan 19)
                df = data[sym]
                if df.empty:
                    f.write("No Data for Jan 19\n")
                    continue
                
                # Use scalar access safely .iloc[0].item()
                try:
                    open_p = df['Open'].iloc[0].item()
                    high_p = df['High'].iloc[0].item()
                    low_p = df['Low'].iloc[0].item()
                    close_p = df['Close'].iloc[0].item()
                    vol = df['Volume'].iloc[0].item()
                except:
                    open_p = df['Open'].iloc[0]
                    high_p = df['High'].iloc[0]
                    low_p = df['Low'].iloc[0]
                    close_p = df['Close'].iloc[0]
                    vol = df['Volume'].iloc[0]
                
                # Prev Day (Jan 16)
                df_prev = prev_data[sym]
                try:
                    prev_close = df_prev['Close'].iloc[-1].item() if not df_prev.empty else open_p
                except:
                    prev_close = df_prev['Close'].iloc[-1] if not df_prev.empty else open_p
                
                # 1. The Pump Context
                day_change_pct = ((close_p - prev_close) / prev_close) * 100
                intraday_high_pct = ((high_p - prev_close) / prev_close) * 100
                
                f.write(f"Prev Close: {prev_close:.2f}\n")
                f.write(f"Jan 19 Open: {open_p:.2f}\n")
                f.write(f"Jan 19 High: {high_p:.2f} (Max Gain: {intraday_high_pct:.2f}%)\n")
                f.write(f"Jan 19 Low:  {low_p:.2f}\n")
                f.write(f"Jan 19 Close: {close_p:.2f} (End Gain: {day_change_pct:.2f}%)\n")
                f.write(f"Volume: {vol}\n")
                
                # 2. Evaluate the "Signal" Context
                # Signal was SHORT.
                
                if "BMW" in sym:
                    sig_price = 54.84
                    stop_price = 55.25
                    f.write(f"Bot Signal: SHORT @ {sig_price}\n")
                    if close_p > sig_price:
                        f.write(f"RESULT: FAILED. Closed higher @ {close_p:.2f}\n")
                    else:
                        f.write(f"RESULT: MIXED/WIN? Closed lower @ {close_p:.2f}\n")
                    if high_p == close_p:
                        f.write("⚠️ WARNING: High == Close. Likely locked in Upper Circuit.\n")
                        
                elif "STYLE" in sym:
                    sig_price = 320.55
                    stop_price = 322.27
                    f.write(f"Bot Signal: SHORT @ {sig_price}\n")
                    if close_p > sig_price:
                        f.write(f"RESULT: FAILED. Closed higher @ {close_p:.2f}\n")
                    else:
                        f.write(f"RESULT: MIXED/WIN? Closed lower @ {close_p:.2f}\n")

                if vol < 100000:
                    f.write("❌ LOW VOLUME WARNING: Stock is illiquid. Traps are common.\n")
                
            except Exception as e:
                f.write(f"Error: {e}\n")

if __name__ == "__main__":
    analyze_losses()
