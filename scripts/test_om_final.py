import pandas as pd
import numpy as np
import logging
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer
from god_mode_logic import GodModeAnalyst

# Configure Logging
logging.basicConfig(level=logging.CRITICAL) # Silence internal logs
logger = logging.getLogger("TestSystem")

def generate_scenario(start_price, end_price, vol_profile="normal"):
    """
    Generates data to create specific VWAP conditions.
    """
    data = []
    current_price = start_price
    
    # Base building (VWAP Anchor)
    for i in range(30):
        data.append({
            'datetime': f"09:{15+i}:00",
            'open': start_price, 'high': start_price+0.1, 'low': start_price-0.1, 'close': start_price,
            'volume': 5000
        })
        
    # The Move
    steps = 20
    step_size = (end_price - start_price) / steps
    
    for i in range(steps):
        current_price += step_size
        data.append({
            'datetime': f"09:{45+i}:00",
            'open': current_price, 'high': current_price+0.1, 'low': current_price-0.1, 'close': current_price,
            'volume': 10000 
        })
        
    # The Stall (Drift)
    for i in range(10):
        data.append({
            'datetime': f"10:{0+i}:00",
            'open': end_price, 'high': end_price+0.1, 'low': end_price-0.1, 'close': end_price,
            'volume': 25000 # High Vol (Absorption)
        })
        
    return pd.DataFrame(data)

def run_analysis_logic(df, label):
    print(f"\n⚡ ANALYZING SCENARIO: {label}")
    print(f"   Price: {df.iloc[-1]['close']:.2f}")
    
    # 1. Init
    tr = TapeReader()
    gm = GodModeAnalyst()
    
    # 2. Check Extension
    vwap_sd = gm.calculate_vwap_bands(df.iloc[:-1])
    print(f"   VWAP Extension: {vwap_sd:.2f} SD")
    
    # 3. Check Tape
    prev_df = df.iloc[:-1]
    is_stalled, msg = tr.detect_stall(prev_df)
    is_absorbed, abs_msg = tr.detect_absorption(prev_df)
    
    tape_signal = is_stalled or is_absorbed
    print(f"   Tape Signal: {'YES' if tape_signal else 'NO'} ({msg or abs_msg})")
    
    # 4. Decision Logic (Replicating analyzer.py)
    is_safe = vwap_sd > 2.0
    
    if tape_signal:
        if is_safe:
             print("   🎯 VERDICT: SHORT SIGNAL CONFIRMED (Extended & Stalled)")
        else:
             print("   🛡️ VERDICT: BLOCKED (Bull Flag / Low Extension)")
    else:
        print("   ❌ NO SIGNAL (No Stall Detected)")

def run_final_test():
    # Scenario A: The 10:04 AM Trap (Price 82)
    # Price moves from 79 to 82. (Small move relative to vol)
    # Expected: Low SD, Blocked.
    df_trap = generate_scenario(start_price=79.0, end_price=82.0)
    run_analysis_logic(df_trap, "10:04 AM Consolidation (Price ~82)")
    
    # Scenario B: The True Top (Price 87)
    # Price moves from 79 to 87. (Huge move)
    # Expected: High SD, Accepted.
    df_top = generate_scenario(start_price=79.0, end_price=87.0)
    run_analysis_logic(df_top, "Day High Top (Price ~87)")

if __name__ == "__main__":
    run_final_test()
