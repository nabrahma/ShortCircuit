import pandas as pd
import numpy as np
import logging

# Set up fake logger for test
logging.basicConfig(level=logging.INFO)

# --- MOCK DATA GENERATOR ---

def generate_stall_data(base_price=100, extension_sd=1.0):
    """
    Generates data that stalls, but controls the VWAP Extension.
    High Extension = Top Reversal Candidate.
    Low Extension = Bull Flag Candidate.
    """
    data = []
    
    # Create history to establish VWAP
    # If we want High Extension, we need price to run away from VWAP.
    # If we want Low Extension, price hugs VWAP.
    
    vwap_target = base_price
    current_price = base_price
    
    # 1. Base building (Get Volume in)
    for i in range(50):
        data.append({
            'datetime': i,
            'open': current_price, 'high': current_price+0.1, 'low': current_price-0.1, 'close': current_price,
            'volume': 1000
        })
        
    # 2. The Move
    # To get high SD, we need price to move fast relative to volume-weighted mean.
    
    move_size = 50 if extension_sd > 1.5 else 5
    
    for i in range(20):
        current_price += (move_size / 20)
        data.append({
            'datetime': 50+i,
            'open': current_price, 'high': current_price+0.5, 'low': current_price-0.1, 'close': current_price+0.4,
            'volume': 5000 # Breakout vol
        })
        
    # 3. The Stall (High Volume, Flat Price)
    stall_price = current_price
    for i in range(10):
        data.append({
            'datetime': 70+i,
            'open': stall_price, 'high': stall_price+0.1, 'low': stall_price-0.1, 'close': stall_price,
            'volume': 20000 # Absorption Vol
        })
        
    return pd.DataFrame(data)

# --- TEST LOGIC ---

def test_extension_filter():
    from analyzer import FyersAnalyzer
    from tape_reader import TapeReader
    from god_mode_logic import GodModeAnalyst
    
    # Mock class to use logic without Fyers object
    class MockAnalyzer(FyersAnalyzer):
        def __init__(self):
            self.tr = TapeReader()
            self.gm = GodModeAnalyst()
            self.fyers = None # Not needed
            
    ana = MockAnalyzer()
    
    print("\n--- TEST 1: BULL FLAG (Low Extension) ---")
    # Should be BLOCKED
    df_flag = generate_stall_data(extension_sd=0.5)
    
    # Manually run the logic snippet from analyzer.py
    prev_df = df_flag.iloc[:-1]
    is_stalled, _ = ana.tr.detect_stall(prev_df)
    
    vwap_sd = ana.gm.calculate_vwap_bands(prev_df)
    print(f"Detected Stall: {is_stalled}")
    print(f"VWAP SD: {vwap_sd:.2f}")
    
    if is_stalled and vwap_sd < 2.0:
        print("✅ PASS: Blocked Short on Bull Flag.")
    else:
        print(f"❌ FAIL: Logic Error (Allowed trade or No Stall).")
        
    print("\n--- TEST 2: BLOWOFF TOP (High Extension) ---")
    # Should be TAKEN
    df_top = generate_stall_data(extension_sd=3.0)
    
    prev_df = df_top.iloc[:-1]
    is_stalled, _ = ana.tr.detect_stall(prev_df)
    vwap_sd = ana.gm.calculate_vwap_bands(prev_df)
    
    print(f"Detected Stall: {is_stalled}")
    print(f"VWAP SD: {vwap_sd:.2f}")
    
    if is_stalled and vwap_sd > 2.0:
        print("✅ PASS: Accepted Short on Extended Top.")
    else:
        print(f"❌ FAIL: Logic Error (Blocked valid trade or No Stall).")

if __name__ == "__main__":
    test_extension_filter()
