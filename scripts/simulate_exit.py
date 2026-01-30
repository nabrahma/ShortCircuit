import pandas as pd

def simulate_exit():
    try:
        df = pd.read_csv('trade_data.csv')
    except Exception:
        print("CSV not found.")
        return

    entry = 1852.7
    sl = 1866.0
    risk = sl - entry
    
    tp1 = entry - (risk * 1.0) # 1:1
    tp2 = entry - (risk * 2.0) # 1:2
    
    print(f"--- SHORT SIMULATION ---")
    print(f"Entry: {entry}")
    print(f"Initial SL: {sl}")
    print(f"TP1 (BE): {tp1:.2f}")
    print(f"TP2 (Trail): {tp2:.2f}")
    
    sl_moved_to_be = False
    trailing = False
    
    prev_high = sl # Initial anchor
    
    exit_price = None
    exit_reason = None
    exit_time = None
    
    for i, row in df.iterrows():
        # Skip the Entry Candle (13:56) for SL Checks
        if i == 0:
            continue
            
        # Parsing time roughly from the long format
        # Format: 2026-01-08 13:56:00+05:30
        t_str = str(row['t']).split(' ')[1][:5] # 13:56
        
        o = float(row['o'])
        h = float(row['h'])
        l = float(row['l'])
        c = float(row['c'])
        
        print(f"[{t_str}] O:{o} H:{h} L:{l} C:{c} | SL: {sl:.2f}")
        
        # 1. Check SL Hit (High triggers SL in Short)
        if h >= sl:
            exit_price = sl
            exit_reason = "SL HIT"
            exit_time = t_str
            print(f"🛑 STOP LOSS HIT at {t_str} @ {sl}")
            break
            
        # 2. Check Exits / Modifications based on Low
        # TP1 Hit -> Move to BreakEven
        if not sl_moved_to_be and l <= tp1:
            sl = entry
            sl_moved_to_be = True
            print(f"  ✅ TP1 Hit ({tp1:.2f})! SL moved to Breakeven.")
            
        # TP2 Hit -> Activate Trailing
        if not trailing and l <= tp2:
            trailing = True
            print(f"  🚀 TP2 Hit ({tp2:.2f})! Trailing Mode ACTIVATED.")
            
        # 3. Trailing Logic
        if trailing:
            # Short Trail: SL = Previous Candle High (locking in gains)
            # Actually, standard is trail ABOVE the high.
            # Use 'prev_high' from PREVIOUS iteration (which is this iter for Next)
            pass
            
        # Update Trail for NEXT candle
        if trailing:
            # Trailing Stop = Last Candle High + Buffer (e.g. 1 point)
            # If current candle High < SL, we survive.
            # Then we lower SL to this candle's High (if lower than current SL)
            potential_sl = h + 1.0
            if potential_sl < sl:
                sl = potential_sl
                print(f"  ⬇️ Trailing SL tightened to {sl:.2f}")
                
    if exit_time:
        print(f"\nRESULT: Exit at {exit_time} @ {exit_price} ({exit_reason})")
        points = entry - exit_price
        print(f"PnL: {points:.2f} points")
    else:
        print(f"\nRESULT: Still Open. Current Price: {c}")
        points = entry - c
        print(f"Unrealized PnL: {points:.2f} points")

if __name__ == "__main__":
    simulate_exit()
