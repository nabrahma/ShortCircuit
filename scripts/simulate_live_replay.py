import time
import pandas as pd
import telebot
from fyers_connect import FyersConnect
import config
import logging
import sys

# Setup Logging
sys.stdout = open("live_replay.log", "w", encoding="utf-8")
sys.stderr = sys.stdout
logging.basicConfig(level=logging.INFO, format='%(asctime)s: %(message)s')

def run_live_replay():
    print("🎬 INITIALIZING LIVE REPLAY...")
    
    # 1. Connect & Fetch Data
    fyers = FyersConnect().authenticate()
    symbol = "NSE:EIMCOELECO-EQ"
    today = "2026-01-08" # Hardcoded to match user's context
    
    data = {
        "symbol": symbol, "resolution": "1", "date_format": "1", 
        "range_from": today, "range_to": today, "cont_flag": "1"
    }
    
    response = fyers.history(data=data)
    if "candles" not in response:
        print("❌ No data found.")
        return

    # Prepare DataFrame
    cols = ["epoch", "open", "high", "low", "close", "volume"]
    df = pd.DataFrame(response["candles"], columns=cols)
    df['t'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
    
    # Filter from 13:56 onwards
    start_time = "13:56"
    mask = df['t'].dt.strftime('%H:%M') >= start_time
    play_df = df[mask].reset_index(drop=True)
    
    if play_df.empty:
        print("❌ No data content after 13:56")
        return

    # 2. Setup Bot
    if not config.TELEGRAM_BOT_TOKEN:
        print("❌ No Telegram Token")
        return
        
    bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
    chat_id = config.TELEGRAM_CHAT_ID

    # 3. Trade State
    entry = 1852.7
    sl = 1866.0
    risk = sl - entry
    tp1 = entry - risk
    tp2 = entry - (2 * risk)
    
    sl_moved_to_be = False
    trailing_active = False
    status = "OPEN"
    
    # Initial Message
    msg = (
        f"🎬 *LIVE SIMULATION STARTED* 🎬\n"
        f"Symbol: *{symbol}*\n"
        f"Time: {start_time} IST (Replay)\n"
        f"Action: *SHORT ENTRY*\n"
        f"Price: *{entry}*\n"
        f"SL: {sl}\n\n"
        f"⏳ *Status*: Waiting for next candle (1 min)..."
    )
    sent_msg = bot.send_message(chat_id, msg, parse_mode="Markdown")
    print(f"✅ Sent Start Message. ID: {sent_msg.message_id}")
    
    # 4. Replay Loop
    # We skip the first row (Entry Candle) for processing updates, 
    # but we wait 60s before showing the *next* candle (13:57).
    
    for i, row in play_df.iterrows():
        if i == 0:
            # We already announced entry at 13:56 candle close.
            # Wait 60s for 13:57 candle to "form".
            print("⏳ Waiting 60s for next candle...")
            time.sleep(60)
            continue
            
        # Current "Live" Candle
        t_str = row['t'].strftime('%H:%M')
        o = row['open']
        h = row['high']
        l = row['low']
        c = row['close']
        
        print(f"[{t_str}] Processing: O:{o} H:{h} L:{l} C:{c}")
        
        # Logic Check
        # Check SL Hit (High)
        if h >= sl:
            status = "SL HIT"
            outcome = f"🛑 STOP LOSS HIT at {sl}"
            # Send Final
            bot.send_message(chat_id, f"🚨 *TRADE CLOSED* ({t_str})\n{outcome}\nExit Price: {sl}", parse_mode="Markdown")
            return

        # Check TP1 (Low)
        if not sl_moved_to_be and l <= tp1:
            sl = entry
            sl_moved_to_be = True
            bot.send_message(chat_id, f"✅ *TP1 HIT* ({t_str})\nLow reached {l}. SL moved to BreakEven ({sl}).", parse_mode="Markdown")
            
        # Check TP2
        if not trailing_active and l <= tp2:
            trailing_active = True
            bot.send_message(chat_id, f"🚀 *TP2 HIT* ({t_str})\nLow reached {l}. Trailing Mode ACTIVATED.", parse_mode="Markdown")
            
        # Trailing Logic (Update SL down)
        if trailing_active:
            potential_sl = h + 1.0 # Loose trail
            if potential_sl < sl:
                sl = potential_sl
                # bot.send_message(chat_id, f"⬇️ Trail Update: SL -> {sl:.2f}") # Optional spam
        
        # Dashboard Update Strategy?
        # User wants "Real Time". 
        # Update the main status every minute.
        
        pnl = entry - c
        emoji = "🟢" if pnl > 0 else "🔴"
        
        update_msg = (
            f"🎬 *LIVE REPLAY: {t_str}*\n"
            f"LTP: *{c}* {emoji}\n"
            f"PnL: *{pnl:.2f} pts*\n"
            f"SL: {sl:.2f} {'(BE)' if sl_moved_to_be else ''}\n"
            f"Day Low: {l}\n"
            f"------------------\n"
            f"Context: {symbol} Short"
        )
        
        try:
            bot.edit_message_text(update_msg, chat_id, sent_msg.message_id, parse_mode="Markdown")
        except Exception as e:
            # Maybe message too old or same content
            pass
            
        # Wait for next candle
        print(f"⏳ Waiting 60s for next candle...")
        time.sleep(60)

if __name__ == "__main__":
    run_live_replay()
