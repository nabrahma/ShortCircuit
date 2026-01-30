import pandas as pd
import pandas_ta as ta
import datetime
import time
import logging
import sys
from fyers_connect import FyersConnect
import config
import telebot

# Redirect output for debugging
sys.stdout = open("sim_log.txt", "w", encoding="utf-8")
sys.stderr = sys.stdout

# Setup Logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Mock Analyzer Logic
def check_setup_simulation(df_history, current_time_str):
    if df_history.empty:
        return None

    # Calculate Indicators on the history available SO FAR
    df = df_history.copy()
    
    # VWAP
    # Suppress warnings if possible, or they go to file
    df['vwap'] = ta.vwap(df['high'], df['low'], df['close'], df['volume'])
    
    latest = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else latest
    
    ltp = latest['close']
    current_vwap = latest['vwap']
    
    if pd.isna(current_vwap):
        return None
    
    # Calculate Logic Vars
    day_high = df['high'].max()
    distance_from_high = (day_high - ltp) / ltp if ltp > 0 else 999
    
    open_p = latest['open']
    close_p = latest['close']
    high_p = latest['high']
    low_p = latest['low']
    
    body = abs(close_p - open_p)
    upper_wick = high_p - max(open_p, close_p)
    lower_wick = min(open_p, close_p) - low_p
    
    # DEBUG: Print status for the crucial minute
    if current_time_str == "13:56":
        print(f"\n--- DEBUG 13:56 ANALYSIS ---")
        print(f"LTP: {ltp} | VWAP: {current_vwap:.2f} (Needs LTP > VWAP)")
        print(f"Day High: {day_high} | Dist: {distance_from_high*100:.2f}% (Needs < 3%)")
        print(f"Candle: Body={body}, Upper={upper_wick}, Ratio={upper_wick/body if body else 0:.2f}")
    
    # Check 1: Price > VWAP (Reversal Logic)
    if ltp < current_vwap:
        if current_time_str == "13:56": print("❌ Rejected: Price below VWAP (Trend already broken?)")
        return None

    # Check 2: Day High Proximity
    if distance_from_high > 0.03: 
        if current_time_str == "13:56": print("❌ Rejected: Too far from Day High")
        return None
        
    pattern_name = ""
    is_bearish = False
    
    # 3a. Shooting Star
    if body > 0 and upper_wick >= 1.5 * body and lower_wick < body:
        is_bearish = True
        pattern_name = "Shooting Star"
        
    # 3b. Bearish Engulfing
    elif open_p > prev['close'] and close_p < prev['open'] and close_p < open_p:
        is_bearish = True
        pattern_name = "Bearish Engulfing"
        
    if is_bearish:
        return {
            'time': current_time_str,
            'symbol': 'NSE:EIMCOELECO-EQ',
            'ltp': ltp,
            'pattern': pattern_name,
            'stop_loss': high_p
        }
    
    if current_time_str == "13:56" and not is_bearish:
        print("❌ Rejected: No Bearish Pattern found")
    
    return None

def run_simulation():
    # 1. Connect
    print("Connecting to Fyers...")
    fyers = FyersConnect().authenticate()
    
    symbol = "NSE:EIMCOELECO-EQ"
    
    # 2. Fetch Full Day History
    print(f"Fetching Data for {symbol}...")
    today = datetime.date.today().strftime("%Y-%m-%d")
    data = {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": today,
        "range_to": today,
        "cont_flag": "1"
    }
    
    response = fyers.history(data=data)
    if "candles" not in response:
        print("❌ No data found.")
        return
        
    cols = ["epoch", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(response["candles"], columns=cols)
    df['datetime'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    
    if not df.empty:
        print(f"Start: {df['datetime'].iloc[0]}")
        print(f"End:   {df['datetime'].iloc[-1]}")
    
    # 3. Replay Loop (13:50 to 14:10)
    start_hour, start_min = 13, 50
    end_hour, end_min = 14, 15
    
    print(f"\n--- ⏳ SIMULATION START ---")
    
    found_signal = False
    
    for i in range(len(df)):
        candle_time = df.iloc[i]['datetime']
        time_str = candle_time.strftime("%H:%M")
        
        # Log flow (Sparse)
        if i % 20 == 0:
            print(f"Iter {i}: {time_str}")

        # Start checking only after 1:50 PM
        if candle_time.hour < start_hour or (candle_time.hour == start_hour and candle_time.minute < start_min):
            continue
            
        # Stop if past end time
        if candle_time.hour > end_hour or (candle_time.hour == end_hour and candle_time.minute > end_min):
            break
            
        df_slice = df.iloc[:i+1]
        signal = check_setup_simulation(df_slice, time_str)
        
        if signal:
            print(f"🚨 ALERT Found at {time_str} | Pattern: {signal['pattern']} | Price: {signal['ltp']}")
            
            # Send Telegram
            if config.TELEGRAM_BOT_TOKEN:
                bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
                msg = (
                    f"🚨 *SIMULATION ALERT* 🚨\n"
                    f"Time: {time_str} (Replay)\n"
                    f"Symbol: *{symbol}*\n"
                    f"Signal: *SHORT* ({signal['pattern']})\n"
                    f"Entry: ₹{signal['ltp']}\n"
                    f"SL: ₹{signal['stop_loss']}\n"
                    f"Analysis: Found reversal at Day High."
                )
                bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
                print("✅ Telegram Sent!")
                found_signal = True
                break 
    
    if not found_signal:
        print("\n❌ No signal found in the simulation window.")

if __name__ == "__main__":
    try:
        run_simulation()
    except Exception as e:
        print(f"CRASH: {e}")
