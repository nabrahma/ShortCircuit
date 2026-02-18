import time
import logging
import threading
from fyers_connect import FyersConnect
import config
import telebot
import datetime
from order_manager import OrderManager
from discretionary_engine import DiscretionaryEngine

# Setup Logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FocusEngine")

class FocusEngine:
    def __init__(self, trade_manager=None, order_manager=None, discretionary_engine=None):
        self.fyers = FyersConnect().authenticate()
        self.trade_manager = trade_manager 
        
        # Phase 41.3: New Core Engines
        self.order_manager = order_manager
        self.discretionary_engine = discretionary_engine
        
        self.active_trade = None # Reference to OrderManager position
        self.is_running = False
        self.bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN) if config.TELEGRAM_BOT_TOKEN else None
        
        # Validation Gate (Phase 37)
        self.pending_signals = {} # {symbol: {signal_data, entry_trigger, invalidation_trigger, timestamp}}
        self.monitoring_active = False
        self.monitor_thread = None
        
        # Auto-Recovery on Init
        self.attempt_recovery()
        
        # Validation Gate (Phase 37)
        self.pending_signals = {} # {symbol: {signal_data, entry_trigger, invalidation_trigger, timestamp}}

    def add_pending_signal(self, signal_data):
        """
        Phase 37: Adds a signal to the Validation Gate.
        It will ONLY be executed if Price breaks Signal Low (Short).
        """
        symbol = signal_data['symbol']
        signal_low = signal_data.get('signal_low')
        if not signal_low:
            logger.error(f"Cannot validate {symbol}: Missing signal_low")
            return

        # Define Triggers
        # Short Logic: Trigger if Price < Low
        entry_trigger = signal_low
        # Invalidate if Price > High (Stop Loss hit before entry)
        invalidation_trigger = signal_data.get('stop_loss', signal_low * 1.01)

        self.pending_signals[symbol] = {
            'data': signal_data,
            'trigger': entry_trigger,
            'invalidate': invalidation_trigger,
            'timestamp': time.time()
        }
        logger.info(f"[GATE] Added {symbol} to Validation Gate. Trigger: < {entry_trigger}")
        
        # Start Background Monitor if not running
        if not self.monitoring_active:
            self.start_pending_monitor()

    def start_pending_monitor(self):
        """Starts the background thread for validation checks."""
        self.monitoring_active = True
        self.monitor_thread = threading.Thread(target=self.monitor_pending_loop, daemon=True)
        self.monitor_thread.start()
        logger.info("[GATE] Validation Monitor Started.")

    def monitor_pending_loop(self):
        """
        Background loop to check pending signals every 2 seconds.
        """
        while self.monitoring_active:
            if not self.pending_signals:
                time.sleep(5) # Sleep longer if empty
                continue
                
            try:
                self.check_pending_signals(self.trade_manager)
            except Exception as e:
                logger.error(f"Monitor Loop Error: {e}")
                
            time.sleep(2) # 2s Interval

    def check_pending_signals(self, trade_manager):
        """
        Phase 37: Monitors pending signals for Validation Trigger.
        Called from Main Loop.
        """
        if not self.pending_signals: return

        # Create copy to avoid runtime error during modification
        current_pending = list(self.pending_signals.items())
        
        for symbol, pending in current_pending:
            try:
                # 1. Fetch LTP
                data = {"symbols": symbol}
                resp = self.fyers.quotes(data=data)
                if 'd' not in resp or not resp['d']: continue
                
                ltp = resp['d'][0]['v']['lp']
                
                trigger_price = pending['trigger']
                inval_price = pending['invalidate']
                timestamp = pending['timestamp']
                
                # A. CHECK TRIGGER (VALIDATION CONFIRMED)
                # For Short: LTP < Trigger (Signal Low)
                if ltp < trigger_price:
                    # =========================================================
                    # AUTO MODE GATE (Phase 42.2.6)
                    # =========================================================
                    auto_enabled = False
                    if hasattr(self, 'telegram_bot') and self.telegram_bot:
                        auto_enabled = self.telegram_bot.is_auto_mode()
                    
                    if not auto_enabled:
                         # Alert-only mode: send signal to Telegram, don't trade
                         logger.info(f"📊 SIGNAL (ALERT ONLY): {symbol} BROKE TRIGGER @ {ltp} | Auto mode OFF")
                         
                         if self.telegram_bot:
                             msg = (
                                 f"📊 **SIGNAL TRIGGERED (MANUAL)**\n\n"
                                 f"Symbol: `{symbol}`\n"
                                 f"Trigger: {trigger_price}\n"
                                 f"LTP: {ltp}\n"
                                 f"**Action: Auto-Trade OFF 🛑**\n\n"
                                 f"Enable with `/auto on` for NEXT signal."
                             )
                             self.telegram_bot.send_alert(msg)
                             
                         # Remove from pending (consumed)
                         del self.pending_signals[symbol]
                         return None

                    logger.info(f"✅ [VALIDATED] {symbol} broke {trigger_price} @ {ltp}. EXECUTING!")
                    
                    # 1. Execute via OrderManager (Phase 41.3)
                    if self.order_manager:
                        pos = self.order_manager.enter_position(pending['data'])
                        if pos:
                            self.start_focus(symbol, pos)
                    else:
                        # Fallback (Legacy)
                        logger.warning("Using Legacy TradeManager (OrderManager not initialized)")
                        self.trade_manager.execute_logic(pending['data'])
                    
                    del self.pending_signals[symbol]
                    return {'status': 'EXECUTED'} # Return result to Main
                    
                # B. CHECK INVALIDATION (STOP HIT BEFORE ENTRY)
                elif ltp > inval_price:
                    logger.info(f"🚫 [INVALIDATED] {symbol} hit {inval_price} before entry. Removed.")
                    del self.pending_signals[symbol]
                    
                # C. TIMEOUT (configurable, default 15 mins — Phase 41.1)
                elif (time.time() - timestamp) > (config.VALIDATION_TIMEOUT_MINUTES * 60):
                     timeout_min = config.VALIDATION_TIMEOUT_MINUTES
                     logger.info(f"⌛ [TIMEOUT] {symbol} pending for >{timeout_min}m. Removed.")
                     del self.pending_signals[symbol]
                     
            except Exception as e:
                logger.error(f"Validation Check Error {symbol}: {e}")
        
        return None

    def attempt_recovery(self):
        """
        Scans Fyers for open positions and pending orders to 'adopt' orphaned trades.
        """
        try:
            logger.info("[RECOVERY] Scanning for orphaned trades...")
            positions = self.fyers.positions()
            
            if 'netPositions' not in positions: return
            
            for p in positions['netPositions']:
                qty = p['netQty']
                if qty != 0:
                    symbol = p['symbol']
                    logger.info(f"[RECOVERY] Found Open Position: {symbol} Qty: {qty}")
                    
                    # Determine Entry Price
                    entry_price = float(p['avgPrice']) # buyAvg or sellAvg depending on side
                    if qty < 0:
                        entry_price = float(p['sellAvg']) # Short Entry
                    
                    # Find Pending SL Order
                    sl_price = entry_price * 1.01 # Default fallback
                    orders = self.fyers.orderbook()
                    if 'orderBook' in orders:
                        for o in orders['orderBook']:
                            if o['symbol'] == symbol and o['status'] == 6: # Pending
                                # Assume this is SL
                                sl_price = float(o['stopPrice']) if o['stopPrice'] > 0 else float(o['limitPrice'])
                                logger.info(f"[RECOVERY] Found Pending SL Order: {sl_price}")
                                break
                    
                    # Start Focus
                    # We pass message_id=None so it sends a new dashboard
                    self.start_focus(symbol, entry_price, sl_price, message_id=None, trade_id="RECOVERY", qty=abs(qty))
                    
                    if self.bot and config.TELEGRAM_CHAT_ID:
                         self.bot.send_message(config.TELEGRAM_CHAT_ID, f"♻️ **RECOVERY MODE**\nAdopting Trade: {symbol}")
                    
                    # We only support 1 active trade for now in Focus Engine
                    break 
                    
        except Exception as e:
            logger.error(f"[RECOVERY] Failed: {e}")

    def start_focus(self, symbol, position_data, message_id=None, trade_id=None, qty=1):
        """
        Latch onto a trade.
        """
        # Adapt to OrderManager state or Legacy
        entry_price = position_data.get('entry_price', position_data.get('entry', 0))
        sl_price = position_data.get('hard_stop_price', position_data.get('sl', 0))
        soft_sl = entry_price * (1 + config.DISCRETIONARY_CONFIG['soft_stop_pct']) if 'entry_price' in position_data else sl_price

        self.active_trade = {
            'symbol': symbol,
            'entry': entry_price,
            'sl': sl_price,       # Hard Stop
            'soft_sl': soft_sl,   # Soft Stop
            'qty': position_data.get('qty', qty),
            'status': 'OPEN',
            'highest_profit': -999,
            'message_id': message_id,
            'trade_id': position_data.get('trade_id_str') if isinstance(position_data, dict) and position_data.get('trade_id_str') else (trade_id or f"Trd_{int(time.time())}"),
            'last_price': entry_price,
            # Phase 41.3 State
            'target_extended': False,
            'current_target': entry_price * (1 - config.DISCRETIONARY_CONFIG['initial_target_pct'])
        }
        
        self.is_running = True
        logger.info(f"[FOCUS] FOCUS MODE ACTIVATED: {symbol} | Entry: {entry_price}")
        
        # Send Initial Dashboard
        self.update_dashboard(initial=True)
        
        # Start Loop
        self.thread = threading.Thread(target=self.focus_loop, daemon=True)
        self.thread.start()

    def _check_broker_position(self, symbol: str) -> dict:
        """Phase 42: Query broker for current position."""
        try:
            positions = self.fyers.positions()
            if positions.get('s') != 'ok' and 'netPositions' not in positions:
                logger.error("[SAFETY] Could not fetch positions")
                return None

            for pos in positions.get('netPositions', []):
                if pos['symbol'] == symbol:
                    return pos

            return None  # Position not found = closed

        except Exception as e:
            logger.error(f"[SAFETY] Broker position check failed: {e}")
            return None

    def focus_loop(self):
        while self.is_running and self.active_trade:
            try:
                symbol = self.active_trade['symbol']

                # ── SAFETY: CHECK IF POSITION CLOSED EXTERNALLY ──────
                if self.order_manager:
                    # Sync with OrderManager state
                    om_pos = self.order_manager.active_positions.get(symbol)
                    if not om_pos or om_pos['status'] != 'OPEN':
                         logger.info(f"[FOCUS] Position closed in OrderManager. Stopping Focus.")
                         self.stop_focus("CLOSED_EXTERNALLY")
                         return
                    
                    # Check Broker Hard Stop Status
                    self.order_manager.monitor_hard_stop_status(symbol)

                # ── CRITICAL: EOD SQUARE-OFF (15:10) ────────────────
                now = datetime.datetime.now()
                if now.hour == 15 and now.minute >= 10:
                    logger.warning(f"⏰ [EOD] Force Closing {symbol} at 15:10")
                    if self.order_manager:
                        self.order_manager.safe_exit(symbol, "EOD_SQUARE_OFF")
                    self.stop_focus("EOD")
                    return

                # 1. Fetch Quote & Process
                data = {"symbols": symbol}
                response = self.fyers.quotes(data=data)
                
                if 'd' in response and len(response['d']) > 0:
                    quote = response['d'][0]
                    qt = quote.get('v', quote)
                    ltp = qt.get('lp')
                    volume = qt.get('volume')
                    avg_price = qt.get('avg_price', ltp)
                    
                    self.active_trade['last_price'] = ltp
                    self.active_trade['volume'] = volume
                    
                    # Update PnL logic
                    entry = self.active_trade['entry']
                    pnl_points = entry - ltp # Short PnL
                    
                    # ── INTELLIGENT EXIT LOGIC (Phase 41.3) ──────────
                    if self.discretionary_engine and self.order_manager:
                        
                        # A. Soft Stop Check
                        soft_sl = self.active_trade['soft_sl']
                        # Short Logic: Price > Soft SL
                        if ltp >= soft_sl: 
                            decision = self.discretionary_engine.evaluate_soft_stop(symbol, self.active_trade)
                            if decision == 'EXIT':
                                self.order_manager.safe_exit(symbol, "SOFT_STOP")
                                return

                        # B. Target Extension Logic
                        target = self.active_trade['current_target']
                        # Short: Price <= Target
                        if ltp <= target:
                            decision = self.discretionary_engine.evaluate_profit_extension(symbol, self.active_trade)
                            if decision == 'TAKE_PROFIT':
                                self.order_manager.safe_exit(symbol, "TARGET_HIT")
                                return
                            elif decision == 'EXTEND':
                                new_target = entry * (1 - config.DISCRETIONARY_CONFIG['extended_target_pct'])
                                self.active_trade['current_target'] = new_target
                                self.active_trade['target_extended'] = True
                                logger.info(f"🚀 [FOCUS] Target Extended to {new_target}")

                    # ── FALLBACK / LEGACY LOGIC ──────────────────────
                    # Keep simplistic trailing if Discretionary Engine not active?
                    # Or just rely on Hard SL (monitored by order_manager)
                    
                    # Hard Stop is handled by `monitor_hard_stop_status` above.
                    
                time.sleep(2)
                self.update_dashboard()
                
            except Exception as e:
                logger.error(f"Focus Loop Error: {e}")
                time.sleep(5)

    def cleanup_orders(self, symbol):
        """
        Cancels all pending orders for the symbol.
        Used to remove Stop Loss orders after exit.
        """
        try:
            orderbook = self.fyers.orderbook()
            if 'orderBook' in orderbook:
                count = 0
                for order in orderbook['orderBook']:
                    if order['symbol'] == symbol and order['status'] in [6]: # 6 = Pending
                        logger.info(f"Cancelling pending Order {order['id']}")
                        self.fyers.cancel_order(data={"id": order['id']})
                        count += 1
                if count > 0:
                    logger.info(f"Cleaned up {count} pending orders for {symbol}")
        except Exception as e:
            logger.error(f"Cleanup Orders Error: {e}")


    def update_dynamic_constraints(self, ltp, day_high, vwap):
        t = self.active_trade
        # Dynamic SL: Above Day High or VWAP, whichever is logical
        # For Short: Max(DayHigh, VWAP) + 0.5% Buffer?
        # Let's be tighter: Day High + 0.1% buffer
        dyn_sl = day_high * 1.001
        
        # If Price is far below Day High, maybe trail to VWAP?
        if ltp < vwap:
            dyn_sl = vwap * 1.002 # Trail above VWAP if we are winning big
            
        t['dynamic_sl'] = round(dyn_sl, 2)
        
        # Dynamic TP: 1.5x VWAP Distance? or just Pivot Points?
        # For now, let's target VWAP crossover if we are above it.
        # If below VWAP, target previous support (mock logic for now without history)
        t['dynamic_tp'] = round(ltp * 0.98, 2) # Arbitrary 2% scalp target for visuals

    def force_refresh(self):
        """
        Manually syncs state with Broker.
        """
        if not self.active_trade: return
        
        try:
            # Check Net Position
            r = self.fyers.positions()
            if 'netPositions' in r:
                symbol = self.active_trade['symbol']
                net_qty = 0
                for p in r['netPositions']:
                    if p['symbol'] == symbol:
                        net_qty = p['netQty']
                        break
                
                # If closed externally
                if net_qty == 0:
                    logger.info("[REFRESH] Position found CLOSED.")
                    self.stop_focus(reason="MANUAL_APP_CLOSE")
                    return
            
            # If still open, just update dashboard
            self.update_dashboard()
            logger.info("[REFRESH] Dashboard Updated.")
            
        except Exception as e:
            logger.error(f"Refresh Error: {e}")

    def update_dashboard(self, initial=False):
        if not self.bot or not config.TELEGRAM_CHAT_ID: return
        
        t = self.active_trade
        if not t: return
        
        entry = t['entry']
        ltp = t['last_price']
        
        # PnL Calc
        pnl_points = entry - ltp
        pnl_cash = pnl_points * t.get('qty', 1) 
        emoji = "🟢" if pnl_points > 0 else "🔴"
        
        # ROI Calculation (5x Leverage)
        # Margin Used = (Price * Qty) / 5
        margin_used = (entry * t.get('qty', 1)) / 5
        roi_pct = (pnl_cash / margin_used) * 100 if margin_used > 0 else 0.0
        
        # Orderflow Indicators (Simplified)
        ba_ratio = t.get('bid_ask_ratio', 1.0)
        ba_sentiment = "Bearish" if ba_ratio < 0.8 else "Bullish" if ba_ratio > 1.2 else "Neutral"
        tape_msg = t.get('tape_alert', "Neutral").replace(" (No Engine)", "")
        
        # Dynamic Levels
        dyn_sl = t.get('dynamic_sl', 0)
        dyn_tp = t.get('dynamic_tp', 0)
        
        # BlackRock Style Dashboard
        msg = (
            f"**{t['symbol']}** | Live P&L (5x)\n"
            f"**Rs.{pnl_cash:,.2f}** {emoji} ({roi_pct:+.2f}%)\n"
            f"_{pnl_points:+.2f} pts_\n\n"
            
            f"LTP: **{ltp}** | Entry: {entry}\n\n"
            
            f"**STATUS**\n"
            f"Action: {tape_msg}\n"
            f"Sentiment: {ba_sentiment} ({ba_ratio})\n\n"
            
            f"**RISK**\n"
            f"Stop: {dyn_sl}\n"
            f"Target: {dyn_tp}\n\n"
            
            f"_[Updated: {datetime.datetime.now().strftime('%H:%M:%S')}]_"
        )
        
        # Buttons
        from telebot import types
        markup = types.InlineKeyboardMarkup()
        trade_id = t.get('trade_id', 'UNKNOWN')
        
        btn_refresh = types.InlineKeyboardButton("🔄 Refresh", callback_data=f"REFRESH_{trade_id}")
        btn_close = types.InlineKeyboardButton("❌ Close Position", callback_data=f"EXIT_{trade_id}")
        
        markup.row(btn_refresh, btn_close)
        
        try:
            if initial or not t['message_id']:
                if t['message_id']:
                     self.bot.edit_message_text(msg, config.TELEGRAM_CHAT_ID, t['message_id'], parse_mode="Markdown", reply_markup=markup)
            else:
                 self.bot.edit_message_text(msg, config.TELEGRAM_CHAT_ID, t['message_id'], parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
            # NETWORK ERROR HANDLING (Crucial for Stability)
            # If Telegram fails, we Log and Continue. We DO NOT CRASH.
            if "message is not modified" in str(e):
                pass # Ignore trivial
            elif "Connection" in str(e) or "HTTPS" in str(e) or "400" in str(e):
                logger.warning(f"[NET] Telegram Update Failed (Retrying next tick): {e}")
            else:
                logger.error(f"Telegram Dashboard Error: {e}")

    def stop_focus(self, reason="STOPPED"):
        # self.update_dashboard() # Final Update (Risk of threading race if called from loop?)
        # Better to update one last time carefully.
        
        trade = self.active_trade
        self.is_running = False
        self.active_trade = None
        logger.info(f"Focus Mode Stopped. Reason: {reason}")
        
        # Phase 20: SFP Watch Trigger
        if reason == "SL_HIT" and trade:
            logger.info("[SFP] SFP WATCH ACTIVATED: Monitoring for Fakeout...")
            # Start SFP Thread
            threading.Thread(target=self.sfp_watch_loop, args=(trade,), daemon=True).start()

    def sfp_watch_loop(self, trade):
        """
        Monitors a stopped trade for 10 minutes.
        If Price crosses back BELOW Entry -> SFP Alert.
        """
        symbol = trade['symbol']
        entry_price = trade['entry']
        start_time = time.time()
        timeout = 600 # 10 Minutes
        
        logger.info(f"SFP Watcher Started for {symbol} (Target < {entry_price})")
        
        while (time.time() - start_time) < timeout:
            try:
                # Fetch Quote
                data = {"symbols": symbol}
                response = self.fyers.quotes(data=data)
                
                if 'd' in response:
                    quote = response['d'][0]['v']
                    ltp = quote.get('lp')
                    
                    # LOGIC: If Price breaks back BELOW Entry (Short Logic)
                    if ltp < entry_price:
                        logger.info(f"[WARN] SFP TRIGGERED: {symbol} is back below {entry_price}")
                        self.send_sfp_alert(trade, ltp)
                        return # Stop Watching
                        
                time.sleep(5)
                
            except Exception as e:
                logger.error(f"SFP Loop Error: {e}")
                time.sleep(5)
                
        logger.info(f"SFP Watch Ended for {symbol} (No Fakeout)")

    def send_sfp_alert(self, trade, ltp):
        if not self.bot or not config.TELEGRAM_CHAT_ID: return
        
        symbol = trade['symbol']
        entry = trade['entry']
        
        msg = (
            f"[WARN] **FAKE OUT DETECTED! (SFP)**\n\n"
            f"[SFP] **{symbol}** trapped buyers!\n"
            f"Price is back below Entry.\n\n"
            f"LTP: *{ltp}*\n"
            f"Key Level: *{entry}*\n\n"
            f"[ACTION] **RE-ENTER SHORT NOW**"
        )
        
        # Send as NEW Message (High Importance)
        self.bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")

