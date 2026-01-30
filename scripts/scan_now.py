import logging
import config
from market_scanner import MarketScanner
import yfinance as yf

# Force Simulation Mode
config.SIMULATION_MODE = True
config.API_KEY = None
config.API_SECRET = None
config.USER_ID = None

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_immediate_scan():
    print("--- Starting Immediate Scan (Nifty 100) ---")
    ms = MarketScanner(None)
    
    # Manually call the internal logic to see raw data
    universe = ms.get_sim_universe()
    print(f"Universe Size: {len(universe)}")
    
    chunk_size = 50
    candidates = []
    
    for i in range(0, len(universe), chunk_size):
        chunk = universe[i:i+chunk_size]
        tickers = [f"{s}.NS" for s in chunk]
        print(f"Scanning Chunk {i//chunk_size + 1}...")
        
        try:
            data = yf.download(tickers, period="2d", group_by='ticker', progress=False)
            
            for sym in chunk:
                try:
                    df = data[f"{sym}.NS"]
                    if df.empty or len(df) < 2: continue
                    
                    prev_close = df['Close'].iloc[-2]
                    ltp = df['Close'].iloc[-1]
                    
                    pct_change = ((ltp - prev_close) / prev_close) * 100
                    
                    # Print everything > 2% just to see
                    if abs(pct_change) > 2.0:
                        print(f" -> {sym}: {pct_change:.2f}%")
                        
                    if pct_change >= 5.0:
                        candidates.append((sym, pct_change))

                except Exception:
                    continue
        except Exception as e:
            print(f"Error: {e}")

    print("\n--- Summary ---")
    if candidates:
        print("Top Gainers (>5%):")
        for sym, chg in candidates:
            print(f"✅ {sym}: +{chg:.2f}%")
    else:
        print("No stocks found > 5%. Market might be flat or data delayed.")

if __name__ == "__main__":
    run_immediate_scan()
