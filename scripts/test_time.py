import pandas as pd
import numpy as np
import logging
from tape_reader import TapeReader
from market_profile import ProfileAnalyzer

# Configure Logging
logging.basicConfig(level=logging.ERROR) # Quiet mode

def find_entry_time():
    data = []
    
    # Phase 1: The Rally (9:15 - 10:00)
    price = 79.0
    for i in range(45):
        price += np.random.uniform(0.1, 0.3)
        data.append({
            'datetime': f"09:{15+i}:00",
            'open': price,
            'high': price + 0.2,
            'low': price - 0.1,
            'close': price + 0.1,
            'volume': np.random.randint(5000, 15000)
        })
        
    # Phase 2: The Stall (10:00 - 10:30)
    # The drift begins at 10:00.
    # The Bot needs 5 candles of "Flatness" to confirm drift.
    high_price = 87.0
    for i in range(30):
        # Time string logic
        minute = i
        time_str = f"10:{minute:02d}:00"
        
        base = 86.8 + np.random.uniform(-0.1, 0.1) # VERY FLAT
        data.append({
            'datetime': time_str,
            'open': base,
            'high': max(base + 0.1, high_price),
            'low': base - 0.1,
            'close': base, 
            'volume': np.random.randint(25000, 45000) # High Vol
        })
        
    df = pd.DataFrame(data)
    
    # Run Simulation Loop
    tr = TapeReader()
    
    print("running simulation minute by minute...")
    
    # Start checking from 10:00 onwards
    for i in range(50, len(df)):
        # Slice data up to current minute
        current_view = df.iloc[:i]
        curr_time = current_view.iloc[-1]['datetime']
        
        is_stalled, msg = tr.detect_stall(current_view)
        is_absorbed, abs_msg = tr.detect_absorption(current_view)
        
        if is_stalled or is_absorbed:
            print(f"🚨 SIGNAL FIRED AT: {curr_time}")
            print(f"   REASON: {msg or abs_msg}")
            print(f"   PRICE: {current_view.iloc[-1]['close']:.2f}")
            break

if __name__ == "__main__":
    find_entry_time()
