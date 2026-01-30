import time
import logging
import threading
import yfinance as yf
import pandas as pd
from telegram_bot import TelegramBot
from focus_engine import FocusEngine
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global to track simulation state
sim_active = False

def simulate_flow():
    symbol = "TBZ.NS"
    bot = TelegramBot()
    focus = FocusEngine()
    
    # 1. Get Price at 09:44 IST
    logger.info("Fetching 09:44 Data...")
    df = yf.download(symbol, period="1d", interval="1m", progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close"}, inplace=True)
    
    # Localize/Convert to find 09:44
    df.index = df.index.tz_convert('Asia/Kolkata')
    target_time = pd.Timestamp.now(tz='Asia/Kolkata').replace(hour=9, minute=44, second=0, microsecond=0)
    
    # Find nearest row
    try:
        # Truncate to minutes for matching
        row = df.iloc[df.index.get_indexer([target_time], method='nearest')[0]]
        price = row['close']
        time_str = row.name.strftime('%H:%M')
    except:
        price = 180.0
        time_str = "09:44"

    # 2. Send Dummy Alert
    fake_signal = {
        "symbol": symbol,
        "ltp": price,
        "gain": 8.5, # Mock
        "pattern": "Simulated Shooting Star",
        "stop_loss": price + 0.5,
        "vwap_dist": 3.2
    }
    
    logger.info(f"Sending Simulation Alert for {time_str} @ {price}")
    bot.send_alert(fake_signal)

    # 3. Define Callback for Button
    def on_click(clicked_symbol):
        global sim_active
        if clicked_symbol == symbol:
            sim_active = True
            logger.info("User clicked button! Starting Focus Mock...")

    # 4. Start Polling
    t = threading.Thread(target=bot.start_polling, args=(on_click,))
    t.daemon = True
    t.start()
    
    # 5. Wait for User Click
    logger.info("Waiting for user to click 'I SHORTED THIS'...")
    wait_count = 0
    while not sim_active:
        time.sleep(1)
        wait_count += 1
        if wait_count % 5 == 0:
            print("... Waiting for click ...")
            
    # 6. Simulate Focus Loop (Trailing SL Demo)
    print("\n--- FOCUS MODE STARTED ---")
    print(f"Tracking {symbol} (Entry: {price})")
    
    # Mock Trailing Scenarios
    # Start: Price drops 1.5% (Should trigger Breakeven Trail)
    print("Market moves in favor... (Drop 1.2%)")
    bot.send_message(f"🔄 <b>UPDATE SL: {symbol}</b>\nNew Level: {price - (price*0.001):.2f}\nReason: Locked Break-Even 🛡️")
    time.sleep(2)

    # Continue: Price drops 2.5% (Should Trail Swing High)
    print("Market crashes... (Drop 2.5%)")
    bot.send_message(f"🔄 <b>UPDATE SL: {symbol}</b>\nNew Level: {price - (price*0.015):.2f}\nReason: Trailing Swing High 📉")
    time.sleep(2)
        
    # Force Exit Message
    print("Triggering Simulated Exit...")
    bot.send_message(
        f"🟢 <b>TAKE PROFIT: {symbol}</b>\n"
        f"Reason: SIMULATION (RSI Oversold)\n"
        f"Price: {price - 4.0:.2f}"
    )
    print("Simulation Complete. Check Telegram for SL updates.")

if __name__ == "__main__":
    simulate_flow()
