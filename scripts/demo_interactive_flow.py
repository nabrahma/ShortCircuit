import time
import pandas as pd
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from fyers_connect import FyersConnect
import config
import logging
import threading
import random

# Logging setup
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("InteractiveDemo")

class InteractiveDemo:
    def __init__(self):
        self.bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.is_focus_active = False
        self.symbol = "NSE:EIMCOELECO-EQ"
        self.entry_price = 1852.7 # Closing of 13:56
        self.sl = 1866.0
        self.data_candles = []
        
        # Setup Handler
        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_query(call):
            if call.data == "confirm_short":
                self.bot.answer_callback_query(call.id, "Target Acquired. Entering FOCUS MODE. 🎯")
                self.is_focus_active = True
                t = threading.Thread(target=self.run_focus_simulation, args=(call.message.message_id,))
                t.start()
            elif call.data == "stop_focus":
                self.bot.answer_callback_query(call.id, "Stopping Focus Mode...")
                self.is_focus_active = False

    def fetch_data(self):
        print("Fetching Historical Data...")
        fyers = FyersConnect().authenticate()
        today = "2026-01-08"
        data = {
            "symbol": self.symbol, "resolution": "1", "date_format": "1", 
            "range_from": today, "range_to": today, "cont_flag": "1"
        }
        response = fyers.history(data=data)
        cols = ["epoch", "open", "high", "low", "close", "volume"]
        df = pd.DataFrame(response["candles"], columns=cols)
        df['t'] = pd.to_datetime(df['epoch'], unit='s').dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
        
        # Get 5 mins starting 13:57 (Next 5 candles after entry)
        mask = (df['t'].dt.strftime('%H:%M') >= "13:57") & (df['t'].dt.strftime('%H:%M') <= "14:02")
        self.data_candles = df[mask].reset_index(drop=True)
        print(f"Loaded {len(self.data_candles)} candles for replay.")

    def run_orbit_alert(self):
        # Clean previous button checks if necessary, but this is a standalone script
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("⚡ I Shorted! (Activate Focus)", callback_data="confirm_short"))
        
        msg_text = (
            f"📡 *ORBIT SCANNER ALERT* 📡\n\n"
            f"Symbol: *{self.symbol}*\n"
            f"Time: 13:56 (Replay)\n"
            f"Pattern: *Shooting Star* at Day High\n"
            f"Price: {self.entry_price}\n"
            f"SL: {self.sl}\n\n"
            f"Waiting for your action..."
        )
        
        self.bot.send_message(self.chat_id, msg_text, reply_markup=markup, parse_mode="Markdown")
        print("✅ Orbit Alert Sent. Waiting for button press...")
        
        # Start Polling (Blocking)
        self.bot.polling(non_stop=True)

    def run_focus_simulation(self, message_id):
        print("🚀 FOCUS MODE STARTED")
        
        current_sl = self.sl
        tp1 = self.entry_price - (self.sl - self.entry_price)
        sl_at_be = False
        
        # We have 1-minute candles. We want to simulate "Liveness" over 60s per candle.
        # We will interpolate mainly just to show movement.
        
        for i, row in self.data_candles.iterrows():
            if not self.is_focus_active:
                print("🛑 Focus Stop Signal Received.")
                break
                
            o, h, l, c = row['open'], row['high'], row['low'], row['close']
            time_str = row['t'].strftime('%H:%M')
            
            ticks = self.generate_ticks(o, h, l, c, 20)
            
            for tick_price in ticks:
                if not self.is_focus_active:
                    break
                    
                # Logic Update
                pnl = self.entry_price - tick_price
                
                # BE Check
                if not sl_at_be and pnl > (self.sl - self.entry_price):
                    current_sl = self.entry_price
                    sl_at_be = True
                    
                # Dashboard Update
                self.update_dashboard(message_id, self.symbol, tick_price, pnl, current_sl, sl_at_be, time_str)
                
                time.sleep(3) # 3s Real-time delay
                
        print("✅ Simulation Ended.")
        self.update_dashboard(message_id, self.symbol, 0, 0, 0, False, "STOPPED") # Explicit stop
        self.bot.stop_bot()

    def generate_ticks(self, o, h, l, c, count):
        # Very rough interpolation for visual effect
        # Path: O -> random -> random -> C, respecting H/L limits
        res = [o]
        for _ in range(count-2):
            # drift towards close but strictly within L/H
            prev = res[-1]
            drift = (c - prev) * 0.1 + (random.random() - 0.5) * 2.0
            new_val = prev + drift
            new_val = max(l, min(h, new_val))
            res.append(new_val)
        res.append(c)
        return res

    def update_dashboard(self, msg_id, symbol, price, pnl, sl, is_be, time_str):
        if time_str == "STOPPED":
             msg = f"🛑 *FOCUS MODE STOPPED*\nTrade Closed by User."
             try:
                self.bot.edit_message_text(msg, self.chat_id, msg_id, parse_mode="Markdown")
             except: pass
             return

        emoji = "🟢" if pnl > 0 else "🔴"
        
        msg = (
            f"🎯 *FOCUS MODE: ON ({time_str})*\n"
            f"Sym: *{symbol}*\n"
            f"------------------\n"
            f"LTP: *{price:.2f}* {emoji}\n"
            f"PnL: *{pnl:.2f} pts*\n"
            f"------------------\n"
            f"⛔ Stop: {sl:.2f} {'(BE)' if is_be else ''}\n"
            f"📉 Target: Open"
        )
        
        markup = InlineKeyboardMarkup()
        markup.add(InlineKeyboardButton("🛑 I Closed It (Stop Focus)", callback_data="stop_focus"))
        
        try:
            self.bot.edit_message_text(msg, self.chat_id, msg_id, reply_markup=markup, parse_mode="Markdown")
        except Exception:
            pass

if __name__ == "__main__":
    demo = InteractiveDemo()
    demo.fetch_data()
    demo.run_orbit_alert()
