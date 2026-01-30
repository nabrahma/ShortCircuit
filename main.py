import time
import logging
import threading
import sys
import config
from fyers_connect import FyersConnect
from scanner import FyersScanner
from analyzer import FyersAnalyzer
from trade_manager import TradeManager
from telegram_bot import ShortCircuitBot

# Logging Setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(config.LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ],
    force=True
)
logger = logging.getLogger(__name__)

def main():
    logger.info("--- ⚡ Starting ShortCircuit (Fyers Edition) ---")

    # 1. Authentication
    fyers_conn = FyersConnect()
    try:
        fyers = fyers_conn.authenticate()
    except Exception as e:
        logger.critical(f"Auth Failed: {e}")
        return

    # 2. Module Initialization
    scanner = FyersScanner(fyers)
    analyzer = FyersAnalyzer(fyers)
    trade_manager = TradeManager(fyers)
    bot = ShortCircuitBot(trade_manager)

    # 3. Start Telegram Thread
    t_bot = threading.Thread(target=bot.start_polling)
    t_bot.daemon = True
    t_bot.start()

    logger.info("✅ System Initialized. Loop Starting...")
    
    # 3.1 Send Motivation
    try:
        bot.send_startup_message()
    except:
        pass

    # 4. Main Loop
    SCAN_INTERVAL = 60 # seconds
    
    while True:
        try:
            logger.info("📡 Scanning Market...")
            start_time = time.time()
            
            # CHECK TIME FOR AUTO-EXIT
            # config.SQUARE_OFF_TIME format "HH:MM" e.g. "15:10"
            import datetime
            now = datetime.datetime.now()
            current_time = now.strftime("%H:%M")
            if current_time >= config.SQUARE_OFF_TIME:
                logger.warning(f"⏰ Market Close Time ({current_time}). Initiating Square-off.")
                msg = trade_manager.close_all_positions()
                
                # Notify Telegram
                try:
                    bot.bot.send_message(bot.chat_id, f"🛑 **MARKET CLOSED**\n\n{msg}\n\nSystem Shutdown. See you tomorrow! 🦅", parse_mode="Markdown")
                except:
                    pass
                    
                break # EXIT MAIN LOOP
            
            # A. Scan
            candidates = scanner.scan_market()
            
            if not candidates:
                logger.info("No volatility found. Sleeping...")
            
            # B. Analyze & Process
            for cand in candidates:
                symbol = cand['symbol']
                ltp = cand['ltp']
                
                # Check Technicals
                signal = analyzer.check_setup(symbol, ltp)
                
                if signal:
                    # C. Execute/Alert
                    result = trade_manager.execute_logic(signal)
                    bot.send_alert(result)
                    
            elapsed = time.time() - start_time
            sleep_time = max(0, SCAN_INTERVAL - elapsed)
            logger.info(f"Cycle finished in {elapsed:.2f}s. Sleeping {sleep_time:.2f}s")
            time.sleep(sleep_time)

        except KeyboardInterrupt:
            logger.info("Manually Stopped.")
            break
        except Exception as e:
            logger.error(f"Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    main()
