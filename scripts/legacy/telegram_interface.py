import logging
import threading
import telebot
from telebot import types
import config
from bot_state import bot_state

logger = logging.getLogger(__name__)

class TelegramInterface:
    def __init__(self):
        self.bot_token = config.TELEGRAM_BOT_TOKEN
        self.chat_id = config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.bot_token and self.chat_id)
        
        if self.enabled:
            self.bot = telebot.TeleBot(self.bot_token)
            self._setup_handlers()
        else:
            self.bot = None

    def _setup_handlers(self):
        """
        Defines the command and callback handlers.
        """
        @self.bot.message_handler(commands=['start', 'help', 'status'])
        def send_welcome(message):
            # Verify user is the admin (security)
            if str(message.chat.id) != str(self.chat_id):
                self.bot.reply_to(message, "⛔ Unauthorized access.")
                return
            
            self._send_control_panel(message.chat.id)

        @self.bot.callback_query_handler(func=lambda call: True)
        def handle_query(call):
            if str(call.message.chat.id) != str(self.chat_id):
                return

            if call.data == "auto_on":
                bot_state.ENABLE_AUTO_TRADE = True
                self.bot.answer_callback_query(call.id, "Auto-Trading ENABLED ✅")
                logger.info("Auto-Trading ENABLED via Telegram.")
                self._send_control_panel(call.message.chat.id)
                
            elif call.data == "auto_off":
                bot_state.ENABLE_AUTO_TRADE = False
                self.bot.answer_callback_query(call.id, "Auto-Trading DISABLED 🛑")
                logger.info("Auto-Trading DISABLED via Telegram.")
                self._send_control_panel(call.message.chat.id)

            elif call.data == "refresh":
                self._send_control_panel(call.message.chat.id)

    def _send_control_panel(self, chat_id):
        """
        Sends the UI Panel with Inline Buttons.
        """
        status = "✅ ACTIVE" if bot_state.ENABLE_AUTO_TRADE else "🛑 MANUAL ONLY"
        markup = types.InlineKeyboardMarkup()
        
        btn_on = types.InlineKeyboardButton("✅ Enable Auto", callback_data="auto_on")
        btn_off = types.InlineKeyboardButton("🛑 Disable Auto", callback_data="auto_off")
        btn_refresh = types.InlineKeyboardButton("🔄 Refresh Status", callback_data="refresh")
        
        markup.row(btn_on, btn_off)
        markup.row(btn_refresh)
        
        self.bot.send_message(
            chat_id, 
            f"🤖 <b>ShortCircuit Control Panel</b>\n"
            f"----------------------------\n"
            f"Current Mode: <b>{status}</b>\n"
            f"----------------------------",
            reply_markup=markup,
            parse_mode="HTML"
        )

    def start_polling(self):
        """
        Starts the bot listener in a separate thread.
        """
        if not self.enabled:
            logger.warning("Telegram keys missing. Interface disabled.")
            return

        def _poll():
            logger.info("Telegram Polling Started...")
            try:
                self.bot.infinity_polling(timeout=10, long_polling_timeout=5)
            except Exception as e:
                logger.error(f"Telegram Polling Crash: {e}")

        # Daemon thread so it closes when main program exits
        t = threading.Thread(target=_poll, daemon=True)
        t.start()
        
        # Send startup msg
        try:
            self._send_control_panel(self.chat_id)
        except:
            pass

    def send_alert(self, symbol, entry, sl, signal="SHORT"):
        """
        Sends trade alert.
        """
        if not self.enabled:
            return
            
        try:
            risk = abs(entry - sl)
            qty = int(config.RISK_PER_TRADE / risk) if risk > 0 else 0
            
            emoji = "⚔️" if bot_state.ENABLE_AUTO_TRADE else "🛡️"
            
            msg = (
                f"{emoji} <b>SIGNAL ALERT: {symbol}</b>\n"
                f"--------------------------\n"
                f"🔴 <b>Type:</b> {signal}\n"
                f"📉 <b>Entry:</b> {entry}\n"
                f"🛑 <b>Stop Loss:</b> {sl}\n"
                f"💰 <b>Risk:</b> ₹{config.RISK_PER_TRADE} (Qty: {qty})\n"
                f"--------------------------\n"
                f"🤖 <i>Status: {'Auto Executed' if bot_state.ENABLE_AUTO_TRADE else 'Manual Signal'}</i>"
            )
            
            self.bot.send_message(self.chat_id, msg, parse_mode="HTML")
            
        except Exception as e:
            logger.error(f"Failed to send alert: {e}")
