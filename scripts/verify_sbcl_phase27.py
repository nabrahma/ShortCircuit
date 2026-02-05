
import sys
import os
import pandas as pd
import datetime
import logging
import time

# Add parent dir to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from analyzer import FyersAnalyzer
from fyers_connect import FyersConnect
import config

logging.basicConfig(level=logging.INFO)

def verify_sbcl_phase27():
    print("--- Verifying SBCL with Phase 27 Logic ---")
    
    # 1. Connect
    fyers_conn = FyersConnect()
    fyers = fyers_conn.authenticate()
    analyzer = FyersAnalyzer(fyers)
    
    symbol = "NSE:SBCL-EQ"
    date_str = "2026-02-03" # The day of the signal
    
    # 2. Fetch History
    print(f"Fetching history for {symbol} on {date_str}...")
    data = {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": date_str,
        "range_to": date_str,
        "cont_flag": "1"
    }
    
    response = fyers.history(data=data)
    
    if "candles" not in response:
        print("❌ Failed to fetch history.")
        return

    cols = ["epoch", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(response["candles"], columns=cols)
    df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    
    # 3. Locate Signal Time (13:26)
    signal_time_str = "2026-02-03 13:26:00+05:30"
    target_idx = -1
    
    for i, row in df.iterrows():
        if str(row['datetime']) == signal_time_str:
            target_idx = i
            break
            
    if target_idx == -1:
        print("❌ Could not find 13:26 candle.")
        # Try approx
        print(f"Available range: {df['datetime'].iloc[0]} to {df['datetime'].iloc[-1]}")
        return

    # Slice Data up to 13:27 (Wait for 13:26 close)
    # The bot runs at 13:27 and looks at the completed 13:26 candle.
    # So we need df up to index (target_idx + 1) to simulate being "at" 13:27 scanning the last candle.
    
    sim_df = df.iloc[:target_idx+1]
    last_candle = sim_df.iloc[-1]
    
    print(f"\nAnalyzing Candle: {last_candle['datetime']}")
    print(f"O: {last_candle['open']}, H: {last_candle['high']}, L: {last_candle['low']}, C: {last_candle['close']}, V: {last_candle['volume']}")
    
    # 4. Enrich Data
    analyzer._enrich_dataframe(sim_df)
    
    # 5. Check Technicals (God Mode)
    print("\n--- Running Logic ---")
    
    # We need to bypass the _check_filters (Time/Regime) as we are simulating past
    # directly call internal logic if possible, or Mock market_context
    analyzer.market_context.is_favorable_time_for_shorts = lambda: (True, "Simulated")
    analyzer.market_context.should_allow_short = lambda: (True, "Simulated")
    
    # Mock OI (Equity has 0)
    oi = 0
    
    # Run Check
    ltp = last_candle['close']
    
    # Manually trigger the Phase 27 checks on this DF
    print("\n--- Phase 27 Module Status ---")
    
    # 5.1 OI Divergence
    # We expect 0 (Skipped)
    analyzer._track_oi(symbol, ltp, 0)
    is_fakeout, oi_msg = analyzer._check_oi_divergence(symbol, ltp)
    print(f"1. OI Divergence: {is_fakeout} ({oi_msg}) [Expected: False/Empty for Equity]")
    
    # 5.2 dPOC Divergence
    dpoc = analyzer.profile_analyzer.get_developing_poc(sim_df)
    is_dpoc_div, dpoc_msg = analyzer._check_dpoc_divergence(symbol, ltp, sim_df)
    print(f"2. dPOC Check: POC={dpoc:.2f}, LTP={ltp}")
    print(f"   Divergence: {is_dpoc_div} ({dpoc_msg})")
    
    # 5.3 Vacuum Test
    avg_vol = sim_df['volume'].iloc[-20:-2].mean()
    curr_vol = sim_df.iloc[-1]['volume'] 
    
    # 6. FORENSIC: What happened next?
    print("\n--- FORENSIC: The Aftermath (Next 15 Mins) ---")
    post_signal_df = df.iloc[target_idx+1:target_idx+16]
    
    # SL Calculation (from Analyzer logic approx)
    # SL = High + ATR Buffer (approx 0.5 to 1.0)
    signal_high = last_candle['high']
    est_sl = signal_high + 1.0 # Crude Estimate
    print(f"Signal High: {signal_high} | Est SL: {est_sl}")
    
    sl_hit = False
    for i, row in post_signal_df.iterrows():
        t = row['datetime'].strftime("%H:%M")
        o, h, l, c = row['open'], row['high'], row['low'], row['close']
        print(f"{t} | O:{o} H:{h} L:{l} C:{c} | Vol:{row['volume']}")
        
        if h >= est_sl:
            print(f"❌ SL HIT at {t} (High: {h})")
            sl_hit = True
            break
            
    if not sl_hit:
        print("✅ SL Survived next 15 mins.")

if __name__ == "__main__":
    verify_sbcl_phase27()
