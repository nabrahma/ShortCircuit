import time
import logging
import threading
from fyers_connect import FyersConnect
from scanner import FyersScanner
from analyzer import FyersAnalyzer
from socket_engine import SocketEngine
import config
import telebot

# Setup Logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("OrbitScanner")

class Orbit:
    def __init__(self):
        self.fyers_obj = FyersConnect()
        self.fyers = self.fyers_obj.authenticate()
        if not self.fyers:
            logger.error("Failed to authenticate Fyers. Orbit cannot start.")
            return
            
        self.scanner = FyersScanner(self.fyers)
        self.analyzer = FyersAnalyzer(self.fyers)
        
        # HFT Engine
        # Need access token from fyers object or config? 
        # Usually fyers object has it internally or we pass it if we stored it.
        # Assuming FyersConnect returns a fyersModel instance which might not expose token directly publicly 
        # depending on version, but let's assume valid token is in config or extracted.
        token = self.fyers_obj.access_token if hasattr(self.fyers_obj, 'access_token') else config.ACCESS_TOKEN
        self.socket_engine = SocketEngine(token)
        
        self.is_running = False
        self.scan_interval = 60 # 1 minute (Sync with Candle Closes)

    def scan_job(self):
        """
        The core loop running in background.
        """
        while self.is_running:
            try:
                logger.info("📡 ORBIT: Starting Scan Cycle...")
                
                # 1. Broad Scan (>8% Gainers, Vol > 100k, LTP > 50)
                # We reuse scanner.py but might need to customize filters dynamically
                # or we just get the list and filter further here.
                
                # Let's get raw candidates first
                candidates = self.scanner.scan_market() # This returns >5% gainers currently
                
                short_candidates = []
                focus_symbols = []
                
                for stock in candidates:
                    # STRICTER FILTERS for "Sniper" Short
                    # 1. Must be extended (>8%)
                    if stock['change'] < 8.0:
                        continue
                        
                    symbol = stock['symbol']
                    ltp = stock['ltp']
                    
                    focus_symbols.append(symbol)
                    
                    # 2. Analyze for Weakness (Pattern)
                    # This fetches 1-min history and checks for Shooting Star / Day High proximity
                    analysis = self.analyzer.check_setup(symbol, ltp)
                    
                    if analysis:
                        logger.info(f"🎯 ORBIT CANDIDATE: {symbol} | {analysis['pattern']}")
                        short_candidates.append(analysis)
                        
                        # FIRE TELEGRAM ALERT
                        self.send_alert(analysis)
                
                # UPDATE HFT ENGINE
                if focus_symbols:
                    self.socket_engine.start(focus_symbols)
                
                logger.info(f"📡 ORBIT: Cycle Complete. HFT Active on {len(focus_symbols)}. Monitoring for triggers...")
                
                # POLLING LOOP (60 seconds)
                # Instead of sleeping, we watch the HFT feed.
                for _ in range(self.scan_interval):
                    if not self.is_running: break
                    
                    # Check HFT Alerts
                    while self.socket_engine.alerts:
                        # Pop the oldest alert
                        alert_msg = self.socket_engine.alerts.pop(0)
                        logger.info(f"⚡ HFT TRIGGER: {alert_msg}")
                        
                        try:
                            # Parse Alert: "👻 SPOOF DETECTED: NSE:OMINFRAL-EQ | ..."
                            parts = alert_msg.split(':', 2) # Grab Symbol part
                            if len(parts) > 1:
                                # Quick cleanup to find symbol
                                # Message format: "TYPE: SYMBOL | MSG"
                                # We construct a signal object for send_alert
                                signal_data = {
                                    'symbol': 'HFT_TARGET', # Placeholder or parse from string
                                    'ltp': 'LIVE',
                                    'pattern': alert_msg,
                                    'meta': 'Real-Time Tick/Depth Analysis',
                                    'stop_loss': 'Review Chart'
                                }
                                self.send_alert(signal_data)
                        except Exception as e:
                            logger.error(f"HFT Alert Parse Error: {e}")
                            
                    time.sleep(1)
                
            except Exception as e:
                logger.error(f"Orbit Error: {e}")
                time.sleep(60) # Failure penalty

    def send_alert(self, signal):
        """
        Notify User of a potential target entering the Kill Zone.
        """
        if config.TELEGRAM_BOT_TOKEN:
            try:
                bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
                msg = (
                    f"📡 *ORBIT SCANNER DETECTED TARGET* 📡\n\n"
                    f"Symbol: *{signal['symbol']}*\n"
                    f"Price: *{signal['ltp']}*\n"
                    f"Pattern: *{signal['pattern']}*\n"
                    f"Data: {signal.get('meta', 'N/A')}\n"
                    f"SL: {signal.get('stop_loss', 'N/A')}\n\n"
                    f"⚡ *God Mode Action*: Verify Setup -> Activate Focus."
                )
                bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Telegram Alert Failed: {e}")

    def start(self):
        if self.is_running:
            logger.warning("Orbit is already running.")
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self.scan_job, daemon=True)
        self.thread.start()
        logger.info("🚀 Orbit Scanner Started in Background.")

    def stop(self):
        self.is_running = False
        self.socket_engine.stop() # Stop HFT Grid
        logger.info("🛑 Orbit Scanner Stopping...")

if __name__ == "__main__":
    # Test Run
    orbit = Orbit()
    orbit.scan_job() # Run once synchronously for test
