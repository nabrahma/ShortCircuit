import pandas as pd
import numpy as np
import logging
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer

# Configure Logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger("TestSystem")

def generate_om_infra_data():
    """
    Generates a synthetic 1-minute dataset for Om Infra (Jan 19, 2026)
    Behavior: Strong Rally -> Consolidation/Drift at Resistance -> Breakdown
    """
    data = []
    
    # Phase 1: The Rally (9:15 - 10:00)
    # Price moves from 79 to 87
    price = 79.0
    for i in range(45):
        price += np.random.uniform(0.1, 0.3)
        data.append({
            'datetime': f"09:{15+i}:00",
            'open': price,
            'high': price + 0.2,
            'low': price - 0.1,
            'close': price + 0.1,
            'volume': np.random.randint(5000, 15000) # Moderate Vol
        })
        
    # Phase 2: The Stall / Absorption (10:00 - 10:30)
    # Price stuck between 86.5 and 87.0 (High)
    # Volume INCREASES (Effort but no result)
    high_price = 87.0
    for i in range(30):
        # Oscillate near high
        base = 86.8 + np.random.uniform(-0.2, 0.2)
        data.append({
            'datetime': f"10:{i}:00",
            'open': base,
            'high': max(base + 0.1, high_price), # Poke high
            'low': base - 0.1,
            'close': base, # Flat close
            'volume': np.random.randint(20000, 40000) # HIGH VOLUME (Absorption)
        })
        
    df = pd.DataFrame(data)
    return df

def run_system_test():
    print("🚀 INITIALIZING NEW SYSTEM ANALYSIS: OM INFRA (OMINFRAL.NS)")
    print("----------------------------------------------------------")
    
    df = generate_om_infra_data()
    print(f"Loaded {len(df)} candles of Intraday Data.")
    print(f"High: {df['high'].max()}, Close: {df.iloc[-1]['close']}")
    
    # Initialize Modules
    tr = TapeReader()
    pa = ProfileAnalyzer()
    
    # 1. TPO / Market Profile Analysis
    print("\n[1] MARKET PROFILE (TPO) SCAN")
    # Calculate Profile
    profile = pa.calculate_tpo_profile(df, price_step=0.1)
    if profile:
        print(f"   > POC (Point of Control): {profile['poc']:.2f}")
        print(f"   > VAH (Value Area High):  {profile['vah']:.2f}")
        print(f"   > VAL (Value Area Low):   {profile['val']:.2f}")
        
        # Check Rejection
        current_ltp = df.iloc[-1]['close']
        is_rejected, msg = pa.check_profile_rejection(df, current_ltp)
        
        status = "🔴 REJECTED" if is_rejected else "🟢 ACCEPTED"
        print(f"   > TPO Signal: {status} ({msg})")
    
    # 2. Tape Reader Analysis
    print("\n[2] TAPE READER (ORDER FLOW) SCAN")
    
    # A. Stall Detection
    is_stalled, stall_msg = tr.detect_stall(df)
    stall_status = "⚠️ DETECTED" if is_stalled else "⚪ NONE"
    print(f"   > Drift/Stall: {stall_status} ({stall_msg})")
    
    # B. Absorption Detection
    is_absorbed, abs_msg = tr.detect_absorption(df)
    abs_status = "⚠️ DETECTED" if is_absorbed else "⚪ NONE"
    print(f"   > Absorption:  {abs_status} ({abs_msg})")
    
    # 3. Final Decision
    print("\n----------------------------------------------------------")
    print("🤖 BOT DECISION (New Logic)")
    
    triggers = []
    if is_stalled: triggers.append("TAPE_STALL")
    if is_absorbed: triggers.append("TAPE_ABSORPTION")
    if is_rejected: triggers.append("PROFILE_REJECTION")
    
    if triggers:
        print(f"✅ SIGNAL FIRED: SHORT OMINFRAL.NS")
        print(f"   Reasons: {', '.join(triggers)}")
    else:
        print("❌ NO TRADE")

if __name__ == "__main__":
    run_system_test()
