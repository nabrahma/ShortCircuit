import pandas as pd
import time
import logging
from fyers_connect import FyersConnect
from scanner import FyersScanner
from god_mode_logic import GodModeAnalyst
import config
import telebot
import sys

# Setup Logging
sys.stdout = open("backtest.log", "w", encoding="utf-8")
sys.stderr = sys.stdout
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("Backtest")

def backtest_today():
    print("🚀 STARTING GOD MODE BACKTEST (Full Day) 🚀")
    
    # 1. Init
    fyers = FyersConnect().authenticate()
    if not fyers: return
    
    scanner = FyersScanner(fyers)
    gm = GodModeAnalyst()
    
    # 2. Get Candidates (End of Day Snapshot)
    # scanner.scan_market() returns list of dicts: {'symbol', 'change', 'ltp', 'volume'}
    print("Scanning Market for Gainers...")
    candidates = scanner.scan_market() # This takes ~30-60s
    
    if not candidates:
        print("No candidates found.")
        return

    print(f"Found {len(candidates)} candidates. Simulating each...")
    
    valid_signals = []
    
    # 3. Deep Simulation Loop
    for stock in candidates:
        symbol = stock['symbol']
        change = stock['change']
        
        # Optimization: Skip if gain < 8%? (Strategy requires > 8%)
        if change < 8.0:
            continue
            
        print(f"Analyzing {symbol} (+{change}%)...")
        
        # Fetch History
        today = "2026-01-08" # Hardcoded today
        data = {
            "symbol": symbol, "resolution": "1", "date_format": "1", 
            "range_from": today, "range_to": today, "cont_flag": "1"
        }
        try:
            resp = fyers.history(data=data)
        except Exception as e:
            print(f"Err fetching {symbol}: {e}")
            continue
            
        if 'candles' not in resp:
            continue
            
        cols = ['epoch','open','high','low','close','volume']
        df = pd.DataFrame(resp['candles'], columns=cols)
        df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
        
        # Calc VWAP
        v = df['volume'].values
        tp = (df['high'] + df['low'] + df['close']) / 3
        df['vwap'] = (tp * v).cumsum() / v.cumsum()
        
        signal_found = False
        
        # Time Loop (11:00 to 15:15)
        for i, row in df.iterrows():
            if row['datetime'].hour < 11: continue
            
            # Slice historical view
            df_slice = df.iloc[:i+1]
            t_str = row['datetime'].strftime('%H:%M')
            ltp = row['close']
            
            # 1. Hard Constraints
            day_high = df_slice['high'].max()
            open_price = df.iloc[0]['open'] # Day Open
            current_gain = ((ltp - open_price) / open_price) * 100
            
            # God Mode Constraint Check
            ok, msg = gm.check_constraints(ltp, day_high, current_gain)
            if not ok: continue
            
            # 2. VWAP Slope (Context)
            if i < 30: continue
            slope, status = gm.calculate_vwap_slope(df_slice.iloc[-30:])
            # Filter? Prefer Flat?
            # User strategy: "If flat trade Reversion".
            # Let's say we accept anything that isn't EXTREME trending up? 
            # Actually, `check_constraints` ensures we are near High.
            # If slope is Massive Up, probably breakout. 
            # Let's trust GodModeAnalyst logic implicitly or just log it.
            
            # 3. Trigger (Structure)
            struct, z_vol = gm.detect_structure(df_slice)
            
            # 4. Snipe Zone
            last_5 = df_slice.iloc[-5:]
            micro_high = last_5['high'].max()
            micro_low = last_5['low'].min()
            range_pos = (ltp - micro_low) / (micro_high - micro_low + 0.001)
            is_sniper_zone = range_pos > 0.70
            
            if "EIMCO" in symbol and "13:50" <= t_str <= "14:00":
                 print(f"DEBUG {t_str}: Gain={current_gain:.1f}% DistHigh={(day_high-ltp)/ltp*100:.2f}% Struct={struct} Zone={is_sniper_zone} ({range_pos:.2f})")
            
            # God Mode Condition:
            # 1. Exhaustion (Shooting Star) -> Valid by itself (it rejected high).
            # 2. Absorption (Doji) -> Must be at High (Sniper Zone) to avoid chop.
            
            valid_trigger = False
            if struct == "EXHAUSTION":
                valid_trigger = True
            elif struct == "ABSORPTION" and is_sniper_zone:
                valid_trigger = True
            
            if valid_trigger:
                # HIT!
                # Ensure we haven't already signalled 5 mins ago?
                # Just take the FIRST valid signal of the day per stock.
                print(f"✅ HIT! {symbol} at {t_str} | {struct} (Z:{z_vol:.1f})")
                
                valid_signals.append({
                    'symbol': symbol,
                    'time': t_str,
                    'price': ltp,
                    'pattern': struct,
                    'gain': current_gain
                })
                signal_found = True
                break # Stop analyzing this stock, we found the entry.
                
    # 4. Reporting
    print(f"✅ Backtest Complete. Found {len(valid_signals)} Trades.")
    
    if config.TELEGRAM_BOT_TOKEN:
        bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
        
        msg = f"📊 **GOD MODE BACKTEST REPORT (Today)** 📊\n\n"
        if valid_signals:
            msg += f"Found **{len(valid_signals)}** Potential Shorts:\n\n"
            for s in valid_signals:
                clean_sym = s['symbol'].replace('NSE:', '').replace('-EQ', '')
                msg += f"• **{clean_sym}** @ {s['time']}\n"
                msg += f"  Gain: {s['gain']:.1f}% | Pat: {s['pattern']}\n"
        else:
            msg += "No God Mode setups found today."
            
        bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")

if __name__ == "__main__":
    backtest_today()
