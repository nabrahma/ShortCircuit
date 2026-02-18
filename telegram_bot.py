import telebot
from telebot import types
import config
import logging
import threading
from journal_manager import JournalManager
from focus_engine import FocusEngine

logger = logging.getLogger(__name__)

class ShortCircuitBot:
    def __init__(self, trade_manager):
        self.bot = telebot.TeleBot(config.TELEGRAM_BOT_TOKEN)
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.trade_manager = trade_manager
        self.journal = JournalManager()
        # Direct Fyers for LTP checks - use existing instance from TradeManager
        self.fyers = trade_manager.fyers
        
        # Focus Engine for Live Dashboard
        self.focus_engine = FocusEngine(trade_manager)
        
        # =====================================================
        # AUTO MODE STATE (Phase 42.2.6)
        # CRITICAL: Always False on init — no exceptions.
        # Only /auto on command can set this to True.
        # =====================================================
        self._auto_mode: bool = False
        
        # Register Handlers
        self.register_handlers()
        
        # Inject self into FocusEngine so it can check auto_mode
        self.focus_engine.telegram_bot = self
        
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
        logger.info("🤖 Telegram Bot initialized | Auto Mode: OFF")

    # ─── Public API ───────────────────────────────────────────

    @property
    def auto_mode(self) -> bool:
        """
        Read-only property for auto mode state.
        """
        return self._auto_mode

    def is_auto_mode(self) -> bool:
        """Alias for auto_mode property (for explicit readability)."""
        return self._auto_mode

    def send_alert(self, message):
        """
        Sends a high-priority alert to the user.
        Used by OrderManager and other critical modules.
        """
        try:
            if self.chat_id:
                self.bot.send_message(self.chat_id, message, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")

    def send_startup_message(self):
        """
        Sends a Good Morning message with a random trading quote.
        """
        import random
        quote = random.choice(self.quotes)
        msg = (
            f"Good Morning, Trader!\n\n"
            f"_{quote}_\n\n"
            f"[BOT] System Online & Scanning..."
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
            self.bot.reply_to(
                message, 
                "[BOT] ShortCircuit (Fyers Edition) Ready.\n\n"
                "Commands:\n"
                "/auto on    — Enable automatic trading\n"
                "/auto off   — Disable auto trading\n"
                "/status     — Show current mode & capital\n"
                "/positions  — List open positions\n"
                "/exit all   — Emergency close all\n"
                "/pnl        — Today's P&L\n"
                "/help       — This message"
            )

        @self.bot.message_handler(commands=['status'])
        def status(message):
            if str(message.chat.id) != str(self.chat_id): return

            mode_str = "🟢 AUTO (trading)" if self._auto_mode else "🔴 ALERT ONLY"
            
            msg = f"*ShortCircuit Status*\n\nMode: {mode_str}\n"

            # Phase 42.1: Capital status
            # Use trade_manager.capital_manager for now, or check if we can access it directly
            if hasattr(self.trade_manager, 'capital_manager'):
                cap = self.trade_manager.capital_manager.get_status()
                msg += (
                    f"Capital: ₹{cap['base_capital']:.0f}\n"
                    f"Buying Power: ₹{cap['total_buying_power']:.0f}\n"
                    f"Open Positions: {cap['positions_count']}\n"
                )
                if cap['positions_count'] > 0:
                    for sym, cost in cap['positions'].items():
                        short_name = sym.split(':')[-1].replace('-EQ', '')
                        msg += f"\n    • {short_name}: ₹{cost:.0f}"
            
            self.bot.reply_to(message, msg, parse_mode="Markdown")

        @self.bot.message_handler(commands=['auto'])
        def toggle_auto(message):
            if str(message.chat.id) != str(self.chat_id): return
            
            args = message.text.split()
            if len(args) > 1:
                cmd = args[1].lower()
                if cmd == 'on':
                    if self._auto_mode:
                        self.bot.reply_to(message, "ℹ️ Auto mode is already ON.")
                        return
                    
                    self._auto_mode = True
                    logger.warning("🟢 AUTO MODE ENABLED by Telegram command")
                    
                    # Try to get buying power for display
                    bp = 0
                    if hasattr(self.trade_manager, 'capital_manager'):
                       bp = self.trade_manager.capital_manager.buying_power
                    
                    self.bot.reply_to(
                        message,
                        f"✅ *AUTO TRADE: ON*\n\n"
                        f"💰 Buying Power: ₹{bp:.0f}\n\n"
                        "Bot will now place orders automatically.\n"
                        "Send /auto off to stop.",
                        parse_mode='Markdown'
                    )
                    
                elif cmd == 'off':
                    if not self._auto_mode:
                        self.bot.reply_to(message, "ℹ️ Auto mode is already OFF.")
                        return

                    self._auto_mode = False
                    logger.warning("🔴 AUTO MODE DISABLED by Telegram command")

                    self.bot.reply_to(
                        message,
                        "🔴 *AUTO TRADE: OFF*\n\n"
                        "Bot is now in alert-only mode.\n"
                        "Signals will be sent but no orders placed.\n"
                        "Send /auto on to re-enable.",
                        parse_mode='Markdown'
                    )
                else:
                    self.bot.reply_to(message, "Usage: /auto on OR /auto off")
            else:
                self.bot.reply_to(message, "Usage: /auto on OR /auto off")

        # ── PHASE 42.2: /WHY COMMAND ──────────────────────────────────
        @self.bot.message_handler(commands=['why'])
        def handle_why(message):
            """Analyze why the bot missed a signal: /why RELIANCE 14:25"""
            args = message.text.split()
            if len(args) < 3:
                self.bot.reply_to(message, "Usage: /why SYMBOL TIME\nExample: /why RELIANCE 14:25")
                return

            symbol = args[1]
            time_str = args[2]

            self.bot.reply_to(message, f"🔍 Running diagnostic for {symbol} @ {time_str}...")

            try:
                from diagnostic_analyzer import DiagnosticAnalyzer
                diag = DiagnosticAnalyzer(self.fyers)
                result = diag.analyze_missed_opportunity(symbol, time_str)

                if 'error' in result:
                    self.bot.send_message(self.chat_id, f"❌ {result['error']}")
                    return

                # Format compact report
                lines = [
                    f"🔍 *Diagnostic: {symbol}*",
                    f"Time: {time_str} | LTP: ₹{result['ltp_at_analysis']:.2f}",
                    f"Day gain: +{result['day_gain']:.1f}% | High: ₹{result['day_high']:.2f}",
                    ""
                ]

                for gate in result['gates']:
                    s = gate['status']
                    icon = '✅' if s == 'PASSED' else ('❌' if s == 'FAILED' else '⚠️')
                    line = f"{icon} G{gate['gate_num']}: {gate['name']}"
                    if s == 'FAILED':
                        line += f"\n    ↳ {gate.get('reason', '')}"
                        if gate.get('suggestion'):
                            line += f"\n    💡 {gate['suggestion'][:100]}"
                    lines.append(line)

                # Verdict
                lines.append("")
                if result['passed_all_gates']:
                    lines.append("✅ PASSED ALL GATES — Signal should have fired!")
                else:
                    fg = result['gates'][result['first_failure_gate'] - 1]
                    lines.append(f"❌ Blocked at Gate {result['first_failure_gate']}: {fg['name']}")

                # Profitability
                prof = result.get('profitability', {})
                if prof.get('available'):
                    lines.append("")
                    if prof['would_be_profitable']:
                        lines.append(f"🎯 Would have profited: +{prof['exit_profit_pct']:.2f}%")
                    else:
                        lines.append(f"💀 Would have lost: {prof['exit_profit_pct']:+.2f}%")

                msg_text = "\n".join(lines)
                # Telegram has 4096 char limit
                if len(msg_text) > 4000:
                    msg_text = msg_text[:4000] + "\n..."

                self.bot.send_message(self.chat_id, msg_text)

            except Exception as e:
                logger.error(f"/why error: {e}")
                self.bot.send_message(self.chat_id, f"❌ Diagnostic error: {str(e)[:200]}")

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
                self.bot.answer_callback_query(call.id, f"[OK] Trade Logged! ID: {trade_id}")
                
                # Update Message to "Tracking Mode"
                new_text = call.message.text
                new_text += f"\n\n[SFP] **ENTRY LOGGED** @ {entry_price}\nStarting Focus Engine..."
                
                # Replace Button with CLOSE
                markup = types.InlineKeyboardMarkup()
                btn_close = types.InlineKeyboardButton("[X] Close Trade", callback_data=f"EXIT_{trade_id}")
                markup.add(btn_close)
                
                # Edit first to acknowledge entry
                sent_msg = self.bot.edit_message_text(chat_id=call.message.chat.id, message_id=call.message.message_id, text=new_text, reply_markup=markup, parse_mode="Markdown")
                
                # START FOCUS ENGINE
                # SL is generic for now (Day High + Buffer done inside engine now or pass generic)
                # We pass same price as initial SL, Engine refines it.
                self.focus_engine.start_focus(symbol, entry_price, entry_price * 1.01, message_id=sent_msg.message_id, trade_id=trade_id)
                
            else:
                 self.bot.answer_callback_query(call.id, "[FAIL] Error logging trade.")

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
                icon = "[+]" if pnl > 0 else "[-]"
                
                self.bot.answer_callback_query(call.id, f"Trade Closed. P&L: {pnl}")
                
                # Send Receipt
                receipt = f"[RECEIPT] **Trade Closed**\n"
                receipt += f"ID: `{trade_id}`\n"
                receipt += f"Entry: {result['entry']}\n"
                receipt += f"Exit: {result['exit']}\n"
                receipt += f"{icon} **P&L**: Rs.{pnl:.2f} ({pct:.2f}%)"
                
                self.bot.send_message(self.chat_id, receipt, parse_mode="Markdown")
                
                # Remove Button to prevent double close
                self.bot.edit_message_reply_markup(chat_id=call.message.chat.id, message_id=call.message.message_id, reply_markup=None)
            else:
                self.bot.answer_callback_query(call.id, "[FAIL] Error closing trade (ID not found?)")

        # 3. REFRESH DASHBOARD
        elif call.data.startswith("REFRESH_"):
            trade_id = call.data.split("_")[1]
            try:
                # 1. Trigger Engine Refresh
                self.focus_engine.force_refresh()
                self.bot.answer_callback_query(call.id, "🔄 Dashboard Refreshed")
            except Exception as e:
                logger.error(f"Refresh Handler Error: {e}")
                self.bot.answer_callback_query(call.id, "[FAIL] Refresh Error")

                
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
                 track_msg = self.bot.send_message(self.chat_id, "[SCAN] Initializing Auto-Trail...")
                 
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
             err_msg = f"[FAIL] **AUTO-TRADE FAILED**\n\nSymbol: `{trade_result.get('msg', 'Unknown')}`\nCheck Logs."
             self.bot.send_message(self.chat_id, err_msg, parse_mode="Markdown")
            
        elif status == "MANUAL_WAIT":
            symbol = self.escape_md(trade_result['symbol'])
            pattern_pretty = self.escape_md(self.prettify_pattern(trade_result['pattern']))
            qty = trade_result['qty']
            ltp = trade_result['ltp']
            sl = trade_result['stop_loss']
            
            # Rich Format
            msg = f"[SIGNAL] *GOD MODE SIGNAL*\n({config.CAPITAL} INR Scalp)\n\n"
            msg += f"`{symbol}`\n" # Monospace for Copy
            msg += f"_(Tap to Copy)_\n\n"
            
            msg += f"[WHY]: {pattern_pretty}\n"
            msg += f"[SIZE]: {qty} Qty\n"
            msg += f"[PRICE]: {ltp}\n"
            msg += f"[STOP]: {sl} (Auto-Calc)\n\n"
            
            msg += f"[ACTION] *Verify Chart & Decide*"

            # Interactive Buttons
            from telebot import types
            markup = types.InlineKeyboardMarkup()
            btn_enter = types.InlineKeyboardButton("[GO] ENTER TRADE", callback_data=f"FOCUS_{trade_result['symbol']}")
            markup.add(btn_enter)
            
            try:
                self.bot.send_message(self.chat_id, msg, parse_mode="Markdown", reply_markup=markup)
            except Exception as e:
                logger.error(f"Failed to send Manual Alert: {e}")

    def send_validation_alert(self, signal):
        """
        Phase 37: Notify user that a signal is in the Validation Gate.
        """
        symbol = self.escape_md(signal['symbol'])
        pattern = self.escape_md(self.prettify_pattern(signal['pattern']))
        trigger = signal.get('signal_low', 0)
        
        msg = (
            f"🛡️ **VALIDATION GATE ACTIVATED**\n\n"
            f"Symbol: `{symbol}`\n"
            f"Pattern: {pattern}\n\n"
            f"**STATUS: PENDING** ⏳\n"
            f"Waiting for Price < **{trigger}**\n"
            f"_(Entry blocked until confirmation)_"
        )
        try:
            self.bot.send_message(self.chat_id, msg, parse_mode="Markdown")
        except Exception as e:
            logger.error(f"Validation Alert Error: {e}")

    def send_multi_edge_alert(self, signal):
        """
        Phase 41: Rich multi-edge alert showing all detected edges.
        """
        symbol = self.escape_md(signal['symbol'])
        edges = signal.get('edges_detected', [])
        confidence = signal.get('confidence', 'HIGH')
        edge_count = signal.get('edge_count', 1)
        trigger = signal.get('signal_low', 0)
        sl = signal.get('stop_loss', 0)

        edge_list = "\n".join([f"  ✓ {self.escape_md(e)}" for e in edges])

        msg = (
            f"🎯 **MULTI\\-EDGE SIGNAL** \\[{confidence}\\]\n\n"
            f"**Symbol:** `{symbol}`\n"
            f"**Primary:** {self.escape_md(signal.get('primary_edge', edges[0] if edges else ''))}\n\n"
            f"**Edges Detected:**\n{edge_list}\n\n"
            f"**Entry:** Below `{trigger}`\n"
            f"**Stop Loss:** `{sl}`\n"
            f"**Confidence:** {confidence} \\({edge_count} edges\\)\n\n"
            f"⏳ _PENDING VALIDATION_"
        )
        try:
            self.bot.send_message(self.chat_id, msg, parse_mode="MarkdownV2")
        except Exception:
            # Fallback to plain Markdown if V2 escaping fails
            plain = (
                f"🎯 **MULTI-EDGE SIGNAL** [{confidence}]\n\n"
                f"Symbol: `{signal['symbol']}`\n"
                f"Edges: {', '.join(edges)}\n"
                f"Entry: Below {trigger} | SL: {sl}\n"
                f"Confidence: {confidence} ({edge_count} edges)\n\n"
                f"STATUS: PENDING VALIDATION"
            )
            try:
                self.bot.send_message(self.chat_id, plain, parse_mode="Markdown")
            except Exception as e:
                logger.error(f"Multi-Edge Alert Error: {e}")

    def send_emergency_alert(self, message: str):
        """
        Phase 42: Send high-priority alert with forced notification.
        Used for critical position errors (duplicate orders, orphaned positions).
        """
        import os
        from datetime import datetime

        emergency_log = getattr(config, 'EMERGENCY_LOG_PATH', 'logs/emergency_alerts.log')

        alert_message = (
            f"⚠️⚠️⚠️ EMERGENCY ALERT ⚠️⚠️⚠️\n\n"
            f"{message}\n\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
            f"Action: Check bot immediately"
        )

        try:
            self.bot.send_message(
                chat_id=self.chat_id,
                text=alert_message,
                disable_notification=False  # Force notification
            )
        except Exception as e:
            logger.critical(f"Could not send emergency alert via Telegram: {e}")

        # Log to emergency log file
        try:
            os.makedirs(os.path.dirname(emergency_log), exist_ok=True)
            with open(emergency_log, 'a') as f:
                f.write(f"{datetime.now()} | {message}\n")
        except Exception as e:
            logger.error(f"Failed to write emergency log: {e}")

    def start_polling(self):
        logger.info("[BOT] Telegram Bot Listening...")
        self.bot.infinity_polling()
