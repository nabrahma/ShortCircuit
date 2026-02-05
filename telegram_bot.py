import telebot
from telebot import types
import config
import logging
import threading
from journal_manager import JournalManager
from fyers_connect import FyersConnect
from focus_engine import FocusEngine

logger = logging.getLogger(__name__)

class ShortCircuitBot:
    def __init__(self, trade_manager):
        self.bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.trade_manager = trade_manager
        self.journal = JournalManager()
        # Direct Fyers for LTP checks if trade_manager doesn't expose it easily
        self.fyers = FyersConnect().authenticate()
        
        # Focus Engine for Live Dashboard
        self.focus_engine = FocusEngine()
        
        # Register Handlers
        self.register_handlers()
        
        self.quotes = [
            "\"The thoughtful trader does not trade every day.\" - Jesse Livermore",
            "\"It's not whether you're right or wrong, but how much money you make when you're right and how much you lose when you're wrong.\" - George Soros",
            "\"Amateurs think about how much money they can make. Professionals think about how much money they could lose.\" - Jack Schwager",
            "\"The market can remain irrational longer than you can remain solvent.\" - John Maynard Keynes",
            "\"Do not anticipate and move without market confirmation—being a little late in your trade is your insurance that you are right or wrong.\" - Jesse Livermore",
            "\"Trading is a waiting game. You sit, you wait, and you make a lot of money all at once.\" - Jim Rogers",
            "\"Cut your losses early and let your profits run.\" - Old Wall Street Adage",
            "\"If you can't take a small loss, sooner or later you will take the mother of all losses.\" - Ed Seykota",
            "\"The goal of a successful trader is to make the best trades. Money is secondary.\" - Alexander Elder",
            "\"Opportunities come infrequently. When it rains gold, put out the bucket, not the thimble.\"",
            "\"Plan the trade, trade the plan.\"",
            "\"There is no holy grail in trading, only risk management.\""
        ]

    def send_startup_message(self):
        """
        Sends a Good Morning message with a random trading quote.
        """
        import random
        quote = random.choice(self.quotes)
        msg = (
            f"☀️ *Good Morning, Trader!* 🦅\n\n"
            f"_{quote}_\n\n"
            f"⚡ System Online & Scanning..."
        )
        try:
            self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")
            logger.info("Startup Motivation Sent.")
        except Exception as e:
            logger.error(f"Failed to send startup msg: {e}")

    def escape_md(self, text):
        """
        Escapes special characters for Markdown to prevent Telegram 400 Errors.
        Chars to escape: _ * [ ] ( ) ~ ` > # + - = | { } . !
        But for simple 'Markdown' (V1), mainly _ and * are tricky if not balanced.
        Let's just replace _ with \_ to be safe as patterns have underscores.
        """
        if not isinstance(text, str): return str(text)
        return text.replace("_", "\\_").replace("*", "\\*").replace("`", "\\`")
        
    def get_ltp(self, symbol):
        try:
            data = {"symbols": symbol}
            response = self.fyers.quotes(data=data)
            if 'd' in response and len(response['d']) > 0:
                return response['d'][0]['v']['lp']
        except:
            pass
        return 0.0

    def register_handlers(self):
        @self.bot.message_handler(commands=['start', 'help'])
        def send_welcome(message):
            self.bot.reply_to(message, "⚡ ShortCircuit (Fyers Edition) Ready.\nCommands:\n/status - Check Bot State\n/auto on - Enable Auto Trading\n/auto off - Disable Auto Trading")

        @self.bot.message_handler(commands=['status'])
        def status(message):
            mode = "🟢 AUTO-TRADE" if self.trade_manager.auto_trade_enabled else "🟡 MANUAL-ALERT"
            self.bot.reply_to(message, f"Status Report:\nMode: {mode}\nScanner: Active")

        @self.bot.message_handler(commands=['auto'])
        def toggle_auto(message):
            args = message.text.split()
            if len(args) > 1:
                cmd = args[1].lower()
                if cmd == 'on':
                    self.trade_manager.set_auto_trade(True)
                    self.bot.reply_to(message, "🚀 Auto-Trading ENABLED. Be careful.")
                elif cmd == 'off':
                    self.trade_manager.set_auto_trade(False)
                    self.bot.reply_to(message, "🛡️ Auto-Trading DISABLED. Switch to Manual Alerts.")
                else:
                    self.bot.reply_to(message, "Usage: /auto on OR /auto off")
            else:
                self.bot.reply_to(message, "Usage: /auto on OR /auto off")

        # Callback for Inline Buttons
        @self.bot.callback_query_handler(func=lambda call: True)
        def callback_handler(call):
            self.handle_query(call)

    def handle_query(self, call):
        """
        Processes callback queries (Entry/Exit Logic).
        Exposed for simulation testing.
        """
        # 1. ENTER TRADE
        if call.data.startswith("FOCUS_"):
            # Data: FOCUS_Symbol
            parts = call.data.split("_")
            symbol = parts[1]
            
            # Fetch Real-time Entry Price
            entry_price = self.get_ltp(symbol)
            if entry_price == 0: entry_price = 100.0 # Fallback safety
            
            # Calc Qty (Re-calc to be sure)
            qty = int(config.CAPITAL / entry_price)
            if qty < 1: qty = 1
            
            # Log to Journal
            trade_id = self.journal.log_entry(symbol, qty, entry_price, "Manual-Telegram")
            
            if trade_id:
                self.bot.answer_callback_query(call.id, f"✅ Trade Logged! ID: {trade_id}")
                
                # Update Message to "Tracking Mode"
                new_text = call.message.text
                new_text += f"\n\n🦅 **ENTRY LOGGED** @ {entry_price}\nStarting Focus Engine..."
                
                # Replace Button with CLOSE
                markup = types.InlineKeyboardMarkup()
                btn_close = types.InlineKeyboardButton("🔴 Close Trade", callback_data=f"EXIT_{trade_id}")
                markup.add(btn_close)
                
                # Edit first to acknowledge entry
                sent_msg = self.bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=new_text, reply_markup=markup, parse_mode="Markdown")
                
                # START FOCUS ENGINE
                # SL is generic for now (Day High + Buffer done inside engine now or pass generic)
                # We pass same price as initial SL, Engine refines it.
                self.focus_engine.start_focus(symbol, entry_price, entry_price * 1.01, message_id=sent_msg.message_id, trade_id=trade_id)
                
            else:
                 self.bot.answer_callback_query(call.id, "❌ Error logging trade.")

        # 2. EXIT TRADE
        elif call.data.startswith("EXIT_"):
            trade_id = call.data.split("_")[1]
            
            # Fetch Real-time Exit Price
            exit_price = 0.0
            
            # Try to get from Focus Engine first (Accurate and Sync)
            if self.focus_engine.active_trade and self.focus_engine.active_trade.get('trade_id') == trade_id:
                 exit_price = self.focus_engine.active_trade['last_price']
            else:
                # Fallback parse
                lines = call.message.text.split('\n')
                symbol_line = [l for l in lines if "NSE:" in l or "BSE:" in l]
                if symbol_line:
                    symbol = symbol_line[0].strip().replace('`','') 
                    exit_price = self.get_ltp(symbol)

            # NOW Stop Focus Engine
            self.focus_engine.stop_focus()
            
            # Clean up Pending Orders (Hard SLs)
            self.focus_engine.cleanup_orders(symbol)
            
            result = self.journal.log_exit(trade_id, exit_price)
            
            if result:
                pnl = result['pnl']
                pct = result['pnl_pct']
                icon = "🟢" if pnl > 0 else "🔴"
                
                self.bot.answer_callback_query(call.id, f"Trade Closed. P&L: {pnl}")
                
                # Send Receipt
                receipt = f"🧾 **Trade Closed**\n"
                receipt += f"🆔 `{trade_id}`\n"
                receipt += f"📉 Entry: {result['entry']}\n"
                receipt += f"📈 Exit: {result['exit']}\n"
                receipt += f"{icon} **P&L**: ₹{pnl:.2f} ({pct:.2f}%)"
                
                self.bot.send_message(self.chat_id, receipt, parse_mode="Markdown")
                
                # Remove Button to prevent double close
                self.bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
            else:
                self.bot.answer_callback_query(call.id, "❌ Error closing trade (ID not found?)")

                
    def prettify_pattern(self, raw_pat):
        if "ABSORPTION" in raw_pat: return "Institutional Absorption (High Vol, Stuck Price)"
        if "EXHAUSTION" in raw_pat: return "Buyer Exhaustion (Shooting Star / Rejection)"
        return raw_pat

    def send_alert(self, trade_result):
        """
        Sends formatted alert based on TradeManager result.
        """
        status = trade_result['status']
        
        if status == "EXECUTED":
             self.bot.send_message(self.chat_id, trade_result['msg'])
             
             # AUTO-START FOCUS ENGINE (Trailing Logic)
             # We need to send a message first to latch onto?
             # Or just use the alert message ID?
             try:
                 # Send a separate "Tracking" dashboard message
                 track_msg = self.bot.send_message(self.chat_id, "📡 Initializing Auto-Trail...")
                 
                 symbol = trade_result['symbol']
                 entry = float(trade_result['ltp'])
                 sl = float(trade_result['sl'])
                 qty = trade_result['qty']
                 trade_id = trade_result['order_id']
                 
                 
                 # Start Focus (Pass corrected Qty and TradeID)
                 self.focus_engine.start_focus(symbol, entry, sl, message_id=track_msg.message_id, trade_id=trade_id, qty=qty)
                 
             except Exception as e:
                 logger.error(f"Failed to start Auto-Focus: {e}")
            
        elif status == "ERROR":
             # Notify User of Failure
             err_msg = f"❌ **AUTO-TRADE FAILED** ❌\n\nSymbol: `{trade_result.get('msg', 'Unknown')}`\nCheck Logs."
             self.bot.send_message(self.chat_id, err_msg, parse_mode="Markdown")
            
        elif status == "MANUAL_WAIT":
            symbol = self.escape_md(trade_result['symbol'])
            pattern_pretty = self.escape_md(self.prettify_pattern(trade_result['pattern']))
            qty = trade_result['qty']
            ltp = trade_result['ltp']
            sl = trade_result['stop_loss']
            
            # Rich Format
            msg = f"🚨 *GOD MODE SIGNAL* 🚨\n({config.CAPITAL} INR Scalp)\n\n"
            msg += f"`{symbol}`\n" # Monospace for Copy
            msg += f"_(Tap to Copy)_\n\n"
            
            msg += f"📊 *Why*: {pattern_pretty}\n"
            msg += f"💰 *Size*: {qty} Qty\n"
            msg += f"🏷️ *Price*: {ltp}\n"
            msg += f"🛑 *Stop*: {sl} (Auto-Calc)\n\n"
            
            msg += f"⚡ *Action Required: Verify Chart & Decide*"

            # Interactive Buttons
            from telebot import types
            markup = types.InlineKeyboardMarkup()
            btn_enter = types.InlineKeyboardButton("🚀 ENTER TRADE", callback_data=f"FOCUS_{trade_result['symbol']}")
            markup.add(btn_enter)
            
            try:
                self.bot.send_message(self.chat_id, msg, parse_mode="Markdown", reply_markup=markup)
            except Exception as e:
                logger.error(f"Failed to send Manual Alert: {e}")

    def start_polling(self):
        logger.info("🤖 Telegram Bot Listening...")
        self.bot.infinity_polling()
