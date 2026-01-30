import threading
import time
import logging
from fyers_apiv3.FyersDataSocket import data_ws

logger = logging.getLogger("SocketEngine")

class SocketEngine:
    def __init__(self, fyers_access_token, log_path="realtime.log"):
        self.access_token = fyers_access_token
        self.log_path = log_path
        self.running = False
        self.ws = None
        self.thread = None
        
        # Data Store
        self.latest_ticks = {} # Symbol -> Last Traded Price/Qty
        self.depth_snapshot = {} # Symbol -> Last Depth State
        
        # Alerts
        self.alerts = []

    def start(self, symbols):
        """
        Starts the WebSocket in a daemon thread.
        symbols: List of symbols to subscribe to (e.g., ["NSE:OMINFRAL-EQ"])
        """
        if self.running: return

        self.custom_symbols = symbols
        self.running = True
        
        # Create Data Socket Instance
        # Standard Fyers API v3 WebSocket
        self.ws = data_ws(
            access_token=self.access_token,
            log_path=self.log_path,
            litemode=False, # Full mode for Depth
            write_to_file=False,
            reconnect=True,
            on_connect=self.on_open,
            on_close=self.on_close,
            on_error=self.on_error,
            on_message=self.on_message
        )
        
        # Start in Thread
        self.thread = threading.Thread(target=self._run_socket, daemon=True)
        self.thread.start()
        logger.info(f"🚀 Socket Engine Started on {len(symbols)} symbols.")

    def _run_socket(self):
        try:
            self.ws.connect()
        except Exception as e:
            logger.error(f"Socket Run Error: {e}")

    def stop(self):
        self.running = False
        if self.ws:
            self.ws.close_connection()
        logger.info("🛑 Socket Engine Stopped.")

    def on_open(self):
        logger.info("Socket Connected.")
        # Subscribe to symbols
        # Data Type: SymbolUpdate (Tick) & DepthUpdate
        # Fyers Type codes: SymbolUpdate=1, DepthUpdate=2
        # We assume standard subscription
        if self.custom_symbols:
            # Subscribe to Depth (Mode 2) and Ticks (Mode 1)?
            # Fyers usually merges them or has different modes.
            # Mode 1: LTP, Mode 2: Quote+Depth.
            # We want Mode 2 (Full Depth)
            data_type = "SymbolUpdate" # Placeholder, actual usually map key
            # self.ws.subscribe(symbol=self.custom_symbols, data_type="symbolData")
            self.ws.subscribe(symbols=self.custom_symbols, data_type="depth") 
            self.ws.subscribe(symbols=self.custom_symbols, data_type="symbolData")

    def on_error(self, message):
        logger.error(f"Socket Error: {message}")

    def on_close(self, message):
        logger.warning(f"Socket Closed: {message}")

    def on_message(self, message):
        """
        Core HFT Logic Gateway
        """
        try:
            # Message structure varies. Assuming simplified dict.
            # { 'symbol': 'NSE:OMINFRAL-EQ', 'ltp': 82.0, 'vol_traded': 500, 'bids': [], 'offers': [], 'type': 'sf' }
            
            # Fyers V3 often returns list of dicts or single dict
            if isinstance(message, list):
                for msg in message:
                    self.process_packet(msg)
            else:
                self.process_packet(message)
                
        except Exception as e:
            logger.error(f"Msg Parse Error: {e}")

    def process_packet(self, msg):
        symbol = msg.get('symbol')
        if not symbol: return
        
        # 1. WHALE WATCH (Tick Logic)
        if 'ltp' in msg and 'last_traded_qty' in msg:
            self.detect_whale(symbol, msg)
            
        # 2. SPOOF WATCH (Depth Logic)
        if 'bids' in msg or 'asks' in msg:
            self.detect_spoof(symbol, msg)

    def detect_whale(self, symbol, msg):
        qty = msg.get('last_traded_qty', 0)
        price = msg.get('ltp', 0)
        
        # Whale Threshold: > 5000 shares in one tick (Tunable)
        if qty > 5000:
            ts = time.strftime("%H:%M:%S")
            alert = f"🐋 WHALE PRINT: {symbol} | {qty} @ {price} | Time: {ts}"
            logger.info(alert)
            self.alerts.append(alert)

    def detect_spoof(self, symbol, msg):
        """
        Detects vanishing walls.
        Compare current Total Sell Qty vs Previous Snapshot.
        """
        current_sell_qty = msg.get('total_sell_qty', 0) # API key varies
        if not current_sell_qty and 'asks' in msg:
             # Sum top 5 asks if total not provided
             current_sell_qty = sum([x['qty'] for x in msg['asks']])
             
        if current_sell_qty == 0: return

        prev_snapshot = self.depth_snapshot.get(symbol)
        
        if prev_snapshot:
            prev_qty = prev_snapshot['sell_qty']
            ltp = msg.get('ltp', prev_snapshot.get('ltp', 0))
            prev_ltp = prev_snapshot.get('ltp', 0)
            
            # Logic:
            # 1. Price moved UP (Approaching Wall)
            # 2. Sell Qty DROPPED significantly (> 40%)
            # 3. No massive trade volume (Hard to verify in same packet, but heuristic)
            
            qty_drop = prev_qty - current_sell_qty
            pct_drop = qty_drop / prev_qty if prev_qty > 0 else 0
            
            price_appproach = ltp > prev_ltp # Price getting closer to asks
            
            if pct_drop > 0.40 and price_appproach:
                 # Check executed volume? 
                 # If executed vol < qty_drop, it was mostly canceled.
                 # Simplified Retail HFT: Just alert on massive drop near price.
                 
                alert = f"👻 SPOOF DETECTED: {symbol} | Wall Vanished (-{int(pct_drop*100)}%) | Qty: {prev_qty}->{current_sell_qty}"
                logger.warning(alert)
                self.alerts.append(alert)
        
        # Update Snapshot
        self.depth_snapshot[symbol] = {
            'sell_qty': current_sell_qty,
            'ltp': msg.get('ltp', 0),
            'time': time.time()
        }
