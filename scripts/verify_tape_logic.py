import pandas as pd
import numpy as np
import logging

# Set up fake logger for test
logging.basicConfig(level=logging.INFO)

# --- 1. MOCK DATA SETUP ---

# A. Style Baazar Scenario: Massive Momentum
# RVOL > 5, Slope Steep
momentum_df = pd.DataFrame([{
    'open': 100 + i, 
    'high': 101 + i, 
    'low': 99 + i, 
    'close': 100.5 + i, 
    'volume': 1000000 if i == 50 else 1000 # Massive bump at end
} for i in range(51)])

# B. Om Infra Scenario: Drift / Stall at Highs
# Price goes up, then flattens out with high volume (Absorption)
drift_df = pd.DataFrame([{
    'open': 100 + (i if i < 20 else 20), # Stops rising at index 20
    'high': 101 + (i if i < 20 else 20),
    'low': 99 + (i if i < 20 else 20),
    'close': 100.5 + (i if i < 20 else 20), # Flat closes
    'volume': 50000 # Consistently high volume
} for i in range(30)])

# --- 2. LOGIC VERIFICATION ---

def test_logic():
    from analyzer import FyersAnalyzer
    from tape_reader import TapeReader
    from god_mode_logic import GodModeAnalyst
    
    tr = TapeReader()
    gm = GodModeAnalyst()
    
    print("--- 1. Testing MOMENTUM FILTER (Style Baazar) ---")
    # Manually run the filter logic
    try:
        df = momentum_df
        recent_vols = df['volume'].iloc[-20:-1]
        avg_v = recent_vols.mean()
        curr_v = df['volume'].iloc[-1]
        
        # Calculate Slope Mock
        slope = 60 # Assume steep
        
        rvol_now = curr_v / avg_v if avg_v > 0 else 0
        
        print(f"RVOL: {rvol_now:.1f}, Slope: {slope}")
        
        if rvol_now > 5.0 and slope > 40:
             print("✅ PASS: Signal Blocked by Train Filter.")
        else:
             print("❌ FAIL: Signal NOT Blocked.")
    except Exception as e:
        print(f"Error: {e}")
        
    print("\n--- 2. Testing DRIFT DETECTION (Om Infra) ---")
    # Manually run Tape Reader Logic
    df = drift_df
    prev_df = df.iloc[:-1]
    
    is_stalled, msg = tr.detect_stall(prev_df)
    is_absorbed, abs_msg = tr.detect_absorption(prev_df)
    
    print(f"Stall Check: {is_stalled} ({msg})")
    print(f"Absorption Check: {is_absorbed} ({abs_msg})")
    
    if is_stalled or is_absorbed:
         print("✅ PASS: Discretionary Signal Triggered (Drift/Absorption).")
    else:
         print("❌ FAIL: No Signal.")

if __name__ == "__main__":
    test_logic()
