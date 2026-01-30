import pandas as pd
import numpy as np
import telebot
from fyers_connect import FyersConnect
from market_profiler import MarketProfiler
from god_mode_logic import GodModeAnalyst
import config
import logging
import sys

# Logging setup
sys.stdout = open("god_mode_sim.log", "w", encoding="utf-8")
sys.stderr = sys.stdout
logging.basicConfig(level=logging.INFO)

def run_god_mode_sim():
    print("⚡ GOD MODE SIMULATION: EIMCOELECO ⚡")
    
    # 1. Connect & Fetch
    fyers = FyersConnect().authenticate()
    symbol = "NSE:EIMCOELECO-EQ"
    today = "2026-01-08"
    
    # 2. Tools
    mp = MarketProfiler(fyers)
    gm = GodModeAnalyst()
    
    data = {
        "symbol": symbol, "resolution": "1", "date_format": "1", 
        "range_from": today, "range_to": today, "cont_flag": "1"
    }
    resp = fyers.history(data=data)
    if 'candles' not in resp:
        print("No Data.")
        return
        
    cols = ['epoch','open','high','low','close','volume']
    df = pd.DataFrame(resp['candles'], columns=cols)
    df['t'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    
    # Calculate VWAP (Full Day)
    v = df['volume'].values
    tp = (df['high'] + df['low'] + df['close']) / 3
    df['vwap'] = (tp * v).cumsum() / v.cumsum()
    
    print(f"Loaded {len(df)} candles.")
    
    # 3. Simulate Day Loop
    in_trade = False
    entry_price = 0
    sl_price = 0
    trade_start_time = ""
    
    bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
    
    # Iterate from 11:00 AM to allow VWAP to settle
    start_idx = 0
    for i, row in df.iterrows():
        if row['t'].hour < 11:
            continue
            
        t_str = row['t'].strftime('%H:%M')
        ltp = row['close']
        # --- DEBUG 13:50-14:00 ---
        if "13:50" <= t_str <= "14:00":
             print(f"[{t_str}] LTP:{ltp} Gain:{gain_pct:.1f}% DistHigh:{(day_high-ltp)/ltp*100:.2f}%")
             
             # Check constraints
             ok, msg = gm.check_constraints(ltp, day_high, gain_pct)
             if not ok: print(f"  ❌ Constraint: {msg}")
             
             if i > 30:
                 slope, status = gm.calculate_vwap_slope(df.iloc[i-30:i+1])
                 print(f"  Slope: {slope:.2f} ({status})")
                 
             struct, z_vol = gm.detect_structure(df.iloc[:i+1])
             print(f"  Structure: {struct} (Z: {z_vol:.2f})")
             
             last_body = abs(row['close'] - row['open'])
             last_wick = row['high'] - max(row['open'], row['close'])
             print(f"  Candle: Body={last_body:.1f}, Wick={last_wick:.1f}")

        # --- ANALYSIS --- (Only if not in trade)
        if not in_trade:
            # 1. Hard Constraints (Ethos)
            day_high = df.iloc[:i+1]['high'].max()
            open_price = df.iloc[0]['open']
            gain_pct = ((ltp - open_price) / open_price) * 100
            
            ok, msg = gm.check_constraints(ltp, day_high, gain_pct)
            if not ok: continue # Skip if not 2% high or <8% gain
            
            # 2. God Mode Context (Bias)
            # VWAP Slope (Last 30 mins)
            if i < 30: continue
            hist_30 = df.iloc[i-30:i+1]
            slope, status = gm.calculate_vwap_slope(hist_30)
            
            # 3. Structure (Trigger)
            # Check Absorption/Exhaustion
            struct, z_vol = gm.detect_structure(df.iloc[:i+1])
            
            # 4. Snipe Entry (Micro Range)
            # Last 5 mins
            last_5 = df.iloc[i-5:i+1]
            micro_high = last_5['high'].max()
            micro_low = last_5['low'].min()
            range_pos = (ltp - micro_low) / (micro_high - micro_low + 0.001)
            
            is_sniper_zone = range_pos > 0.70 # Top 30%
            
            signal_valid = False
            reason = ""
            
            if struct in ["ABSORPTION", "EXHAUSTION"] and is_sniper_zone:
                signal_valid = True
                reason = f"{struct} (Z: {z_vol:.1f}) @ Top 25% Range"
            
            # DEBUG REJECTION
            if "13:56" == t_str and not signal_valid:
                 print(f"  ❌ REJECTED 13:56: Struct={struct}, Zone={is_sniper_zone} ({range_pos:.2f})")

            if signal_valid:
                # ENTRY
                in_trade = True
                entry_price = ltp
                sl_price = micro_high + 1.0 # Tight Stop just above micro range
                trade_start_time = t_str
                
                print(f"✅ ENTRY at {t_str} | {entry_price} | {reason}")
                
                # Send TG
                bot.send_message(config.TELEGRAM_CHAT_ID, 
                    f"⚡ **GOD MODE SIGNAL** ⚡\n"
                    f"Time: {t_str}\n"
                    f"Symbol: EIMCOELECO\n"
                    f"Signal: **SHORT** ({reason})\n"
                    f"Price: {entry_price}\n"
                    f"VWAP Slope: {slope:.3f} ({status})\n"
                    f"Micro-Range: {range_pos*100:.0f}% (Sniper Zone)",
                    parse_mode="Markdown"
                )
                
        # --- MANAGEMENT --- (If in trade)
        else:
            # Simple Management for Simulation
            # Exit if SL Hit
            if row['high'] >= sl_price:
                in_trade = False
                pnl = entry_price - sl_price
                print(f"🛑 SL HIT at {t_str} | PnL: {pnl}")
                bot.send_message(config.TELEGRAM_CHAT_ID, f"🛑 SL Hit at {t_str}. PnL: {pnl:.2f}")
                continue
                
            # Exit if Target (Risk:Reward 1:3 or Volume Profile Support)
            risk = abs(sl_price - entry_price)
            pnl = entry_price - row['low']
            
            if pnl > (3 * risk):
                in_trade = False
                exit_price = entry_price - (3 * risk)
                print(f"✅ TP HIT at {t_str} | PnL: {3*risk}")
                bot.send_message(config.TELEGRAM_CHAT_ID, f"✅ TP Hit (1:3) at {t_str}. PnL: {3*risk:.2f}")
                
            # God Mode Exit? (Absorption at Bottom?)
            # Leaving simple for now to prove entry logic.

if __name__ == "__main__":
    run_god_mode_sim()
