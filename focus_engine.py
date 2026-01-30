import time
import logging
import threading
from fyers_connect import FyersConnect
import config
import telebot
import datetime

# Setup Logger
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FocusEngine")

class FocusEngine:
    def __init__(self):
        self.fyers = FyersConnect().authenticate()
        self.active_trade = None # {symbol, entry, sl, tp1, tp2, quantity, start_time, message_id}
        self.is_running = False
        self.bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN) if config.TELEGRAM_BOT_TOKEN else None

    def start_focus(self, symbol, entry_price, sl_price, message_id=None, trade_id=None):
        """
        Latch onto a trade.
        """
        risk = abs(entry_price - sl_price)
        tp1 = entry_price - risk if entry_price < sl_price else entry_price + risk # 1:1
        tp2 = entry_price - (2 * risk) if entry_price < sl_price else entry_price + (2 * risk) # 1:2
        
        self.active_trade = {
            'symbol': symbol,
            'entry': entry_price,
            'sl': sl_price,
            'initial_sl': sl_price,
            'tp1': tp1,
            'tp2': tp2,
            'status': 'OPEN',
            'sl_at_be': False,
            'trailing_active': False,
            'highest_profit': -999,
            'message_id': message_id,
            'trade_id': trade_id,
            'last_price': entry_price
        }
        
        self.is_running = True
        logger.info(f"🎯 FOCUS MODE ACTIVATED: {symbol} | Entry: {entry_price} | ID: {trade_id}")
        
        # Send Initial Dashboard
        self.update_dashboard(initial=True)
        
        # Start Loop
        self.thread = threading.Thread(target=self.focus_loop, daemon=True)
        self.thread.start()

    def focus_loop(self):
        while self.is_running and self.active_trade:
            try:
                symbol = self.active_trade['symbol']
                
                # 1. Fetch Quote (Fast)
                data = {"symbols": symbol}
                response = self.fyers.quotes(data=data)
                
                if 'd' in response and len(response['d']) > 0:
                    quote = response['d'][0]
                    # V3 Structure
                    qt = quote.get('v', quote) # Handle nested or flat structure
                    ltp = qt.get('lp')
                    volume = qt.get('volume')
                    avg_price = qt.get('avg_price', ltp) # VWAP
                    total_buy = qt.get('total_buy_qty', 0)
                    total_sell = qt.get('total_sell_qty', 0)
                    day_high = qt.get('high_price', ltp)
                    
                    self.active_trade['last_price'] = ltp
                    self.active_trade['vwap'] = avg_price
                    self.active_trade['volume'] = volume
                    
                    # Orderflow Stats
                    self.active_trade['bid_ask_ratio'] = round(total_buy / total_sell, 2) if total_sell > 0 else 1.0
                    self.active_trade['vwap_dist'] = round(((ltp - avg_price) / avg_price) * 100, 2)
                    
                    # Dynamic Logic
                    self.update_dynamic_constraints(ltp, day_high, avg_price)
                    
                    self.process_tick(ltp, volume, total_buy, total_sell)
                    
                time.sleep(2) # 2s Interval for 'pulsing' feel
                self.update_dashboard()
                
            except Exception as e:
                logger.error(f"Focus Loop Error: {e}")
                time.sleep(5)

    def analyze_tape(self, tick_data):
        """
        Quant Tape Reading via FootprintCalculator.
        """
        if not hasattr(self, 'footprint_calc'):
            from orderflow_engine import FootprintCalculator
            self.footprint_calc = FootprintCalculator()
            
        # Tick Data: {timestamp, ltp, vol}
        # We need to construct this from what we have. 
        # Focus Engine has LTP and Volume.
        # But 'volume' from Quote is Cumulative Day Volume usually?
        # Fyers 'volume' in quote is 'volume_traded_today'.
        # We need LAST tick volume.
        
        t = self.active_trade
        current_vol = tick_data.get('volume', 0)
        
        prev_vol = t.get('last_day_volume', current_vol)
        tick_vol = current_vol - prev_vol
        if tick_vol < 0: tick_vol = 0 # reset?
        
        t['last_day_volume'] = current_vol
        
        # Feed Engine
        raw_tick = {
            'ltp': tick_data['ltp'],
            'vol': tick_vol,
            'timestamp': datetime.datetime.now()
        }
        
        self.footprint_calc.process_tick(raw_tick)
        
        # Check Absorption
        is_absorption = self.footprint_calc.check_absorption()
        
        tape_msg = "Neutral ⚖️"
        if is_absorption:
            tape_msg = "⚠️ ABSORPTION (Delta Div)"
            
        # Maintain "Wall" check from Liquidity Map?
        # We can also check Walls here if we have depth data in Focus Engine loop.
        # Focus Engine loop currently fetches Quotes. 
        # Making it fetch Depth every 2s might be rate limit heavy?
        # Let's stick to Footprint here.
        
        t['tape_alert'] = tape_msg
        return tape_msg

    def process_tick(self, ltp, volume, total_buy, total_sell):
        trade = self.active_trade
        
        # Prepare Data for Tape
        tick_data = {'ltp': ltp, 'volume': volume}
        self.analyze_tape(tick_data)
        
        entry = trade['entry']
        current_sl = trade['sl']
        
        # Calculate PnL (Short)
        pnl_points = entry - ltp
        
        # Track Highest Profit
        if pnl_points > trade['highest_profit']:
            trade['highest_profit'] = pnl_points
            
        # 1. Check SL HIT (Hard or Trailing)
        if ltp >= current_sl:
            logger.warning(f"🛑 SL HIT: {ltp} (Stop: {current_sl})")
            trade['status'] = 'SL HIT'
            
            # EXECUTE EXIT
            try:
                # Close Position (Buy Market)
                data = {
                    "symbol": trade['symbol'],
                    "qty": trade.get('qty', 1),
                    "type": 2, # Market
                    "side": 1, # Buy
                    "productType": "INTRADAY",
                    "limitPrice": 0,
                    "stopPrice": 0,
                    "validity": "DAY",
                    "disclosedQty": 0,
                    "offlineOrder": False
                }
                self.fyers.place_order(data=data)
                
                # Notify User
                if self.bot and config.TELEGRAM_CHAT_ID:
                    self.bot.send_message(config.TELEGRAM_CHAT_ID, f"🛑 **STOP LOSS TRIGGERED**\n\n{trade['symbol']} hit stop at {ltp}.\nPosition Closed.")
                    
            except Exception as e:
                logger.error(f"Failed to Auto-Exit on SL: {e}")

            self.stop_focus(reason="SL_HIT")
            return

        # 2. TP1 (BreakEven) Logic
        risk = abs(entry - trade['initial_sl'])
        if not trade['sl_at_be'] and pnl_points >= risk:
            trade['sl'] = entry
            trade['sl_at_be'] = True
            logger.info("✅ Moves to BreakEven")

        # 3. TP2 (Trailing) Logic
        if not trade['trailing_active'] and pnl_points >= (2 * risk):
            trade['trailing_active'] = True
            logger.info("🚀 Trailing Activated")
            
        # 4. Dynamic Trailing
        if trade['trailing_active']:
            potential_sl = ltp + (risk * 0.5) 
            if potential_sl < current_sl:
                trade['sl'] = potential_sl
                logger.info(f"⬇️ Trail Tightened to {potential_sl}")


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
        
        # Orderflow Indicators
        ba_ratio = t.get('bid_ask_ratio', 1.0)
        ba_sentiment = "Bearish 🐻" if ba_ratio < 0.8 else "Bullish 🐂" if ba_ratio > 1.2 else "Neutral ⚖️"
        
        vwap_dist = t.get('vwap_dist', 0)
        vwap_status = "Extended 🚀" if vwap_dist > 1.5 else "Mean Rev 🧘"
        
        # Tape Alert
        tape_msg = t.get('tape_alert', "Neutral ⚖️")
        
        msg = (
            f"🎯 *LIVE FOCUS: {t['symbol']}* 🎯\n"
            f"-----------------------------\n"
            f"💰 P&L: *₹{pnl_cash:.2f}* {emoji} ({pnl_points:.2f} pts)\n"
            f"📉 LTP: *{ltp}* (Entry: {entry})\n"
            f"-----------------------------\n"
            f"📼 *Tape Reading (Quant)*\n"
            f"• Action: *{tape_msg}*\n"
            f"• Sentiment: *{ba_sentiment}* ({ba_ratio})\n"
            f"-----------------------------\n"
            f"📊 *Stats*\n"
            f"• VWAP Dist: *{vwap_dist}%* ({vwap_status})\n"
            f"• Vol Spike: {'⚠️ YES' if t.get('vol_spike') else 'No'}\n"
            f"-----------------------------\n"
            f"🛡️ *Smart Constraints*\n"
            f"• Dyn SL: *{t.get('dynamic_sl', 0)}* (Sugg)\n"
            f"• Dyn TP: *{t.get('dynamic_tp', 0)}* (Liq)\n"
            f"-----------------------------\n"
            f"Updated: {datetime.datetime.now().strftime('%H:%M:%S')}"
        )
        
        # Keyboard is managed by the Bot (Close Button), we just edit text.
        # BUT, if we edit text, we might lose the markup if we don't pass it again?
        # Telebot edit_message_text removes markup if not provided?
        # Actually usually it keeps it if reply_markup is Not specified? 
        # No, usually it clears it. We need to preserve the button.
        # We need to construct the "Close Trade" button here too.
        
        from telebot import types
        markup = types.InlineKeyboardMarkup()
        # trade_id is needed for the callback.
        # We need to store trade_id in active_trade
        trade_id = t.get('trade_id', 'UNKNOWN')
        btn = types.InlineKeyboardButton("🔴 Close Trade & Capture", callback_data=f"EXIT_{trade_id}")
        markup.add(btn)
        
        try:
            if initial or not t['message_id']:
                # Initial send is handled by Bot usually? 
                # No, flow is: Bot sends Alert -> User Clicks -> Bot edits to "Logged".
                # THEN we want to START flashing this message.
                # So we are editing the SAME message ID.
                if t['message_id']:
                     self.bot.edit_message_text(msg, config.TELEGRAM_CHAT_ID, t['message_id'], parse_mode="Markdown", reply_markup=markup)
            else:
                 self.bot.edit_message_text(msg, config.TELEGRAM_CHAT_ID, t['message_id'], parse_mode="Markdown", reply_markup=markup)
        except Exception as e:
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
            logger.info("🦅 SFP WATCH ACTIVATED: Monitoring for Fakeout...")
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
                        logger.info(f"⚠️ SFP TRIGGERED: {symbol} is back below {entry_price}")
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
            f"⚠️ **FAKE OUT DETECTED! (SFP)** ⚠️\n\n"
            f"🦅 **{symbol}** trapped buyers!\n"
            f"Price is back below Entry.\n\n"
            f"📉 LTP: *{ltp}*\n"
            f"🧱 Key Level: *{entry}*\n\n"
            f"⚡ **RE-ENTER SHORT NOW** ⚡"
        )
        
        # Send as NEW Message (High Importance)
        self.bot.send_message(config.TELEGRAM_CHAT_ID, msg, parse_mode="Markdown")

if __name__ == "__main__":
    # Test
    pass
