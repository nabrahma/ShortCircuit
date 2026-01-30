import yfinance as yf
import pandas as pd
import pandas_ta as ta
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def debug_yfinance():
    symbol = "ADANIENT"
    tickers = [f"{symbol}.NS", "TATASTEEL.NS"]
    
    logger.info(f"Downloading {tickers}...")
    # Matches market_scanner logic
    data = yf.download(tickers, period="2d", group_by='ticker', progress=False)
    
    print("Data Type:", type(data))
    print("Columns:", data.columns)
    
    try:
        df = data[f"{symbol}.NS"]
        print("DF Type:", type(df))
        print("DF Columns:", df.columns)
        
        prev_close = df['Close'].iloc[-2]
        ltp = df['Close'].iloc[-1]
        print(f"Prev: {prev_close}, LTP: {ltp}")
        
        pct_change = ((ltp - prev_close) / prev_close) * 100
        print(f"Change: {pct_change}%")
        
        if pct_change > 5.0:
            print("Passes Filter")
            
        # Matches main.py -> data_engine.py logic
        print("\n--- OHLC Logic ---")
        df_ohlc = yf.download(f"{symbol}.NS", period="1d", interval="1m", progress=False)
        print("OHLC Empty?", df_ohlc.empty)
        if not df_ohlc.empty:
            df_ohlc.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)
            print("Renamed Cols:", df_ohlc.columns)
            
            # Indicator
            print("Calculating VWAP...")
            try:
                df_ohlc.ta.vwap(append=True)
                print("VWAP Calc Success")
                print(df_ohlc.tail())
            except Exception as e:
                print(f"VWAP Error: {e}")

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_yfinance()
