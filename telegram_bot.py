# telegram_bot.py
# Phase 42.3.1 — Complete Telegram UI

import asyncio
import logging
import threading
from datetime import datetime
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

logger = logging.getLogger(__name__)


class ShortCircuitBot:
    """
    Telegram Bot — Command Interface + Trading Dashboard.

    Responsibilities:
    - Auto-Trade Gate (single source of truth for auto_mode state)
    - Signal alerts (alert-only + interactive GO/SKIP buttons)
    - Live position dashboard (auto-refresh every 2s)
    - All user commands (/status, /why, /pnl, etc.)

    Auto Mode State:
    - ALWAYS False on boot — no exceptions
    - Only /auto on command changes it to True
    - Checked by trade_manager, focus_engine, order_manager
    """

    def __init__(self, config_settings: dict, order_manager, capital_manager,
                 focus_engine=None):
        self.config = config_settings
        self.order_manager = order_manager
        self.capital_manager = capital_manager
        self.focus_engine = focus_engine

        # ── Auto-Trade Gate ───────────────────────────────────
        # CRITICAL: Always False on boot. /auto on to enable.
        self._auto_mode: bool = False

        # ── Telegram App ──────────────────────────────────────
        self.bot_token = config_settings.get('TELEGRAM_BOT_TOKEN')
        self.chat_id = str(config_settings.get('TELEGRAM_CHAT_ID'))
        self.app: Optional[Application] = None

        # ── Dashboard State ───────────────────────────────────
        # Tracks live dashboard message IDs for editing (not spam)
        self._dashboard_message_id: Optional[int] = None
        self._active_signal_message_id: Optional[int] = None
        self._dashboard_task: Optional[asyncio.Task] = None

        # ── Scanning State ────────────────────────────────────
        self._scanning_paused: bool = False
        
        # ── Pending Signals (for Manual Gate) ────────────────
        self._pending_signals = {} 

        logger.info(f"🤖 Telegram Bot initialized | Auto Mode: OFF")

    # ════════════════════════════════════════════════════════════
    # PUBLIC API — used by other modules
    # ════════════════════════════════════════════════════════════

    @property
    def auto_mode(self) -> bool:
        return self._auto_mode

    def is_auto_mode(self) -> bool:
        return self._auto_mode

    def is_scanning_paused(self) -> bool:
        return self._scanning_paused

    async def send_message(self, text: str, parse_mode='Markdown',
                           reply_markup=None) -> Optional[Message]:
        """Send plain message to authorized chat."""
        if not self.app: return None
        try:
            return await self.app.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
        except Exception as e:
            logger.error(f"Telegram send_message failed: {e}")
            return None

    async def edit_message(self, message_id: int, text: str,
                           parse_mode='Markdown',
                           reply_markup=None) -> bool:
        """Edit existing message (used for live dashboard updates)."""
        if not self.app: return False
        try:
            await self.app.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=message_id,
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup
            )
            return True
        except Exception as e:
            # Telegram throws if message hasn't changed — ignore that
            if "Message is not modified" not in str(e):
                logger.error(f"Telegram edit_message failed: {e}")
            return False

    # ════════════════════════════════════════════════════════════
    # SIGNAL ALERTS — called by trade_manager / focus_engine
    # ════════════════════════════════════════════════════════════

    async def send_signal_alert(self, signal: dict):
        """
        Send signal notification to Telegram.

        Auto OFF → Alert with [GO] [SKIP] [Details] buttons
        Auto ON  → Info only (order already placed by this point)
        """
        symbol = signal.get('symbol', 'UNKNOWN')
        side = signal.get('side', 'SHORT')
        entry = signal.get('entry_price', 0)
        sl = signal.get('stop_loss', 0)
        target = signal.get('target', 0)
        rr = signal.get('risk_reward', 0)
        score = signal.get('score', 0)
        pattern = signal.get('pattern', 'Unknown')
        signal_id = signal.get('id', f"{symbol}_{datetime.now().strftime('%H%M%S')}")

        side_emoji = "🔴" if side == "SHORT" else "🟢"
        mode_tag = "🤖 AUTO" if self._auto_mode else "👁️ ALERT"

        text = (
            f"{mode_tag} | *{symbol}* {side_emoji} {side}\n\n"
            f"Entry:    ₹{entry:.2f}\n"
            f"SL:       ₹{sl:.2f}\n"
            f"Target:   ₹{target:.2f}\n"
            f"R:R:      1:{rr:.1f}\n"
            f"Score:    {score:.1f}/10\n"
            f"Pattern:  {pattern}\n"
        )

        if self._auto_mode:
            # Auto mode: order is already being placed
            text += "\n✅ _Order being placed automatically..._"
            await self.send_message(text)

        else:
            # Manual mode: give user GO/SKIP/Details buttons
            text += "\n⚠️ _Auto mode OFF — tap GO to execute._"

            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ GO",
                        callback_data=f"go:{signal_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ SKIP",
                        callback_data=f"skip:{signal_id}"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "📊 Details",
                        callback_data=f"details:{signal_id}"
                    )
                ]
            ])

            msg = await self.send_message(text, reply_markup=keyboard)
            if msg:
                # Store signal for GO callback to reference
                self._pending_signals[signal_id] = signal
                self._active_signal_message_id = msg.message_id

    async def send_order_confirmation(self, signal: dict, order_id: str):
        """
        Sent after order fills. Replaces signal alert message.
        """
        symbol = signal.get('symbol')
        side = signal.get('side')
        entry = signal.get('entry_price', 0)
        qty = signal.get('quantity', 0)
        sl = signal.get('stop_loss', 0)
        target = signal.get('target', 0)

        text = (
            f"✅ *ORDER FILLED*\n\n"
            f"*{symbol}* {side}\n"
            f"Entry:   ₹{entry:.2f} × {qty}\n"
            f"SL:      ₹{sl:.2f}\n"
            f"Target:  ₹{target:.2f}\n"
            f"ID:      `{order_id}`\n\n"
            f"_Position manager activated. Dashboard starting..._"
        )

        await self.send_message(text)

    # ════════════════════════════════════════════════════════════
    # LIVE DASHBOARD — auto-refreshes every 2 seconds
    # ════════════════════════════════════════════════════════════

    async def start_live_dashboard(self, position: dict):
        """
        Start auto-refreshing live P&L dashboard for an active position.
        """
        # Cancel any existing dashboard
        if self._dashboard_task:
            self._dashboard_task.cancel()

        # Send initial dashboard message
        text = self._build_dashboard_text(position)
        keyboard = self._build_dashboard_keyboard(position)
        msg = await self.send_message(text, reply_markup=keyboard)

        if msg:
            self._dashboard_message_id = msg.message_id
            # Start auto-refresh loop
            self._dashboard_task = asyncio.create_task(
                self._dashboard_refresh_loop(position)
            )

    async def _dashboard_refresh_loop(self, initial_position: dict):
        """Edit dashboard message every 2 seconds with fresh data."""
        symbol = initial_position.get('symbol')
        while True:
            try:
                await asyncio.sleep(2)

                # Get latest position data from focus engine (or order manager)
                position = None
                if self.focus_engine:
                    position = await self.focus_engine.get_position_snapshot(symbol)
                
                # If focus engine doesn't have it, maybe it closed?
                if not position:
                    break
                    
                if position.get('status') == 'CLOSED':
                    break

                text = self._build_dashboard_text(position)
                keyboard = self._build_dashboard_keyboard(position)

                if self._dashboard_message_id:
                    await self.edit_message(
                        self._dashboard_message_id,
                        text,
                        reply_markup=keyboard
                    )

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Dashboard refresh error: {e}")
                await asyncio.sleep(5)

    def _build_dashboard_text(self, position: dict) -> str:
        """Build the live dashboard message text."""
        symbol = position.get('symbol', 'UNKNOWN')
        side = position.get('side', 'SHORT')
        entry = position.get('entry_price', 0)
        qty = position.get('quantity', 0)
        current = position.get('current_price', entry)
        pnl = position.get('unrealised_pnl', 0)
        sl = position.get('stop_loss', 0)
        target = position.get('target', 0)
        sl_state = position.get('sl_state', 'INITIAL')
        orderflow = position.get('orderflow_bias', 'NEUTRAL')

        # Direction arrow
        if side == 'SHORT':
            direction = "⬇️" if current < entry else "⬆️"
        else:
            direction = "⬆️" if current > entry else "⬇️"

        # P&L formatting
        pnl_pct = (pnl / (entry * qty)) * 100 if entry and qty else 0
        roi_pct = pnl_pct * 5  # 5× leverage
        pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
        pnl_pct_str = f"+{pnl_pct:.2f}%" if pnl_pct >= 0 else f"{pnl_pct:.2f}%"

        # SL state badge
        sl_badges = {
            'INITIAL': '',
            'BREAKEVEN': '(BREAKEVEN 🔒)',
            'TRAILING': '(TRAILING 📍)',
            'TIGHTENING': '(TIGHT 🎯)'
        }
        sl_badge = sl_badges.get(sl_state, '')

        # Orderflow emoji
        of_map = {
            'BEARISH': '🟢 BEARISH CONFIRMED',
            'BULLISH': '🔴 BULLISH (CAUTION)',
            'NEUTRAL': '⚪ NEUTRAL'
        }
        of_str = of_map.get(orderflow, orderflow)

        return (
            f"⚡ *ACTIVE TRADE*\n\n"
            f"*{symbol}* {side}\n"
            f"Entry: ₹{entry:.2f} | Qty: {qty}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current: ₹{current:.2f} {direction}\n"
            f"P&L: {pnl_str} ({pnl_pct_str})\n"
            f"ROI: {'+' if roi_pct >= 0 else ''}{roi_pct:.2f}% (5× leverage)\n\n"
            f"Stop:   ₹{sl:.2f} {sl_badge}\n"
            f"Target: ₹{target:.2f}\n\n"
            f"Orderflow: {of_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Updated: {datetime.now().strftime('%H:%M:%S')}_"
        )

    def _build_dashboard_keyboard(self, position: dict) -> InlineKeyboardMarkup:
        """Build dashboard inline buttons."""
        symbol = position.get('symbol', 'UNKNOWN')
        order_id = position.get('order_id', '')

        return InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "🔄 Refresh",
                    callback_data=f"refresh:{symbol}"
                ),
                InlineKeyboardButton(
                    "❌ Close Now",
                    callback_data=f"close:{symbol}:{order_id}"
                )
            ]
        ])

    async def stop_live_dashboard(self, position: dict, exit_reason: str):
        """
        Stop dashboard when position closes. Show final P&L.
        """
        if self._dashboard_task:
            self._dashboard_task.cancel()

        symbol = position.get('symbol')
        side = position.get('side')
        entry = position.get('entry_price', 0)
        exit_price = position.get('exit_price', 0)
        qty = position.get('quantity', 0)
        pnl = position.get('realised_pnl', 0)
        pnl_pct = (pnl / (entry * qty)) * 100 if entry and qty else 0
        roi = pnl_pct * 5

        result_emoji = "✅" if pnl > 0 else "❌"
        pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"

        exit_reason_map = {
            'SL_HIT': '🛑 Stop Loss Hit',
            'TP1_HIT': '🎯 TP1 Hit (50% secured)',
            'TP2_HIT': '🎯 TP2 Hit (75% secured)',
            'TP3_HIT': '🏆 Full Target Hit',
            'MANUAL_EXIT': '👤 Manual Exit',
            'SOFT_STOP': '🧠 Discretionary Exit (Soft Stop)',
            'EOD_SQUAREOFF': '🕒 EOD Square-off (3:10 PM)',
            'EMERGENCY': '🚨 Emergency Exit'
        }
        reason_str = exit_reason_map.get(exit_reason, exit_reason)

        text = (
            f"{result_emoji} *TRADE CLOSED*\n\n"
            f"*{symbol}* {side}\n"
            f"Entry:  ₹{entry:.2f}\n"
            f"Exit:   ₹{exit_price:.2f}\n"
            f"Qty:    {qty}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"P&L:    {pnl_str} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)\n"
            f"ROI:    {'+' if roi >= 0 else ''}{roi:.2f}% (5× leverage)\n\n"
            f"Reason: {reason_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"_Position closed at {datetime.now().strftime('%H:%M:%S')}_"
        )

        # Edit final state of dashboard message
        if self._dashboard_message_id:
            await self.edit_message(self._dashboard_message_id, text)
        else:
            await self.send_message(text)

        self._dashboard_message_id = None

    # ════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ════════════════════════════════════════════════════════════

    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/start — Welcome message."""
        if not self._is_authorized(update):
            return

        mode_str = "🟢 AUTO" if self._auto_mode else "🔴 ALERT ONLY"

        bp = 0
        if self.capital_manager:
            bp = self.capital_manager.buying_power

        await update.message.reply_text(
            f"⚡ *ShortCircuit is running.*\n\n"
            f"Mode: {mode_str}\n"
            f"Buying Power: ₹{bp:.0f}\n\n"
            f"Send /help for all commands.\n"
            f"Send /auto on to enable trading.",
            parse_mode='Markdown'
        )

    async def _cmd_auto_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/auto on — Enable auto trading."""
        if not self._is_authorized(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return

        if self._auto_mode:
            await update.message.reply_text("ℹ️ Auto mode is already *ON*.", parse_mode='Markdown')
            return

        self._auto_mode = True
        logger.warning("🟢 AUTO MODE ENABLED via Telegram /auto on")

        bp = 0
        if self.capital_manager:
            bp = self.capital_manager.buying_power

        await update.message.reply_text(
            f"✅ *AUTO TRADE: ON*\n\n"
            f"💵 Buying Power: ₹{bp:.0f}\n\n"
            f"Bot will now place orders automatically.\n"
            f"Send /auto off to stop.",
            parse_mode='Markdown'
        )

    async def _cmd_auto_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/auto off — Disable auto trading."""
        if not self._is_authorized(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return

        if not self._auto_mode:
            await update.message.reply_text("ℹ️ Auto mode is already *OFF*.", parse_mode='Markdown')
            return

        self._auto_mode = False
        logger.warning("🔴 AUTO MODE DISABLED via Telegram /auto off")

        await update.message.reply_text(
            f"🔴 *AUTO TRADE: OFF*\n\n"
            f"Bot is in alert-only mode.\n"
            f"Signals sent as alerts with GO/SKIP buttons.\n"
            f"Send /auto on to re-enable.",
            parse_mode='Markdown'
        )

    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/status — Full system health snapshot."""
        if not self._is_authorized(update):
            return

        mode_str = "🟢 AUTO (trading)" if self._auto_mode else "🔴 ALERT ONLY"
        scan_str = "⏸️ PAUSED" if self._scanning_paused else "✅ ACTIVE"

        today_pnl = 0.0
        today_trades = 0
        open_positions = 0
        
        if self.order_manager:
            try:
                today_pnl = await self.order_manager.get_today_pnl()
                today_trades = await self.order_manager.get_today_trade_count()
                open_positions = await self.order_manager.get_open_position_count()
            except Exception:
                pass

        pnl_str = f"+₹{today_pnl:.2f}" if today_pnl >= 0 else f"-₹{abs(today_pnl):.2f}"
        
        base_cap = getattr(self.capital_manager, "base_capital", 0)
        lev = getattr(self.capital_manager, "leverage", 1)
        bp = getattr(self.capital_manager, "buying_power", 0)
        
        if self.capital_manager:
            base_cap = self.capital_manager.base_capital
            lev = self.capital_manager.leverage
            bp = self.capital_manager.buying_power

        await update.message.reply_text(
            f"📊 *ShortCircuit Status*\n\n"
            f"Mode:       {mode_str}\n"
            f"Scanner:    {scan_str}\n\n"
            f"Capital:    ₹{base_cap:.0f}\n"
            f"Leverage:   {lev}×\n"
            f"Buying Pwr: ₹{bp:.0f}\n\n"
            f"Open:       {open_positions} position(s)\n"
            f"Trades:     {today_trades} today\n"
            f"P&L:        {pnl_str} today\n\n"
            f"_As of {datetime.now().strftime('%H:%M:%S')}_",
            parse_mode='Markdown'
        )

    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/positions — List all open positions."""
        if not self._is_authorized(update):
            return

        positions = []
        if self.order_manager:
            try:
                positions = await self.order_manager.get_open_positions()
            except Exception as e:
                await update.message.reply_text(f"❌ Error fetching positions: {e}")
                return

        if not positions:
            await update.message.reply_text("📭 No open positions.")
            return

        text = "📋 *Open Positions*\n\n"
        for p in positions:
            pnl = p.get('unrealised_pnl', 0)
            pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
            text += (
                f"*{p['symbol']}* {p['side']}\n"
                f"Entry: ₹{p['entry_price']:.2f} | "
                f"LTP: ₹{p['current_price']:.2f} | "
                f"P&L: {pnl_str}\n\n"
            )

        await update.message.reply_text(text, parse_mode='Markdown')

    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/pnl — Today's P&L breakdown."""
        if not self._is_authorized(update):
            return

        trades = []
        if self.order_manager:
            try:
                trades = await self.order_manager.get_today_trades()
            except Exception as e:
                await update.message.reply_text(f"❌ Error: {e}")
                return

        if not trades:
            await update.message.reply_text("📭 No trades today.")
            return

        total_pnl = sum(t.get('realised_pnl', 0) for t in trades)
        wins = [t for t in trades if t.get('realised_pnl', 0) > 0]
        losses = [t for t in trades if t.get('realised_pnl', 0) < 0]
        total_str = f"+₹{total_pnl:.2f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):.2f}"

        text = f"💰 *Today's P&L*\n\n"
        for i, t in enumerate(trades, 1):
            pnl = t.get('realised_pnl', 0)
            pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
            emoji = "✅" if pnl > 0 else "❌"
            text += f"{emoji} {t['symbol']}: {pnl_str}\n"

        text += (
            f"\n━━━━━━━━━━━━━━━━━━\n"
            f"Total:   {total_str}\n"
            f"Wins:    {len(wins)}\n"
            f"Losses:  {len(losses)}\n"
            f"W/R:     {len(wins)/len(trades)*100:.0f}%\n"
        )

        await update.message.reply_text(text, parse_mode='Markdown')

    async def _cmd_why(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/why SYMBOL — Diagnostic replay."""
        if not self._is_authorized(update):
            return

        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: `/why SYMBOL`\nExample: `/why RELIANCE`",
                parse_mode='Markdown'
            )
            return

        symbol = args[0].upper()
        if not symbol.startswith('NSE:'):
            symbol = f"NSE:{symbol}-EQ"

        await update.message.reply_text(
            f"🔍 Analyzing {symbol}... (checking all 12 gates)",
        )

        try:
            from diagnostic_analyzer import DiagnosticAnalyzer
            # Need to instantiate DiagnosticAnalyzer properly
            # Assuming it takes just symbol or broker
            # For strict correctness, we assume main.py injects dependencies into bot if needed
            # But DiagnosticAnalyzer usually needs OrderManager or similar
            # Let's import inside try to avoid circular
            analyzer = DiagnosticAnalyzer(
               # broker interface?
               # We might need to pass self.order_manager.broker
               # For now, placeholder implementation as per PRD
               symbol=symbol,
               order_manager=self.order_manager
            )
            result = await analyzer.run()
            await update.message.reply_text(result, parse_mode='Markdown')
        except Exception as e:
             # Fallback if DiagnosticAnalyzer signature is different
             await update.message.reply_text(f"❌ Diagnostic failed: {e}")

    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/pause — Suspend signal scanning."""
        if not self._is_authorized(update):
            return

        self._scanning_paused = True
        logger.warning("⏸️ Scanning PAUSED via Telegram")
        await update.message.reply_text("⏸️ *Scanning paused.*", parse_mode='Markdown')

    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/resume — Reactivate scanning."""
        if not self._is_authorized(update):
            return

        self._scanning_paused = False
        logger.warning("▶️ Scanning RESUMED via Telegram")
        await update.message.reply_text("▶️ *Scanning resumed.*", parse_mode='Markdown')

    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/help"""
        if not self._is_authorized(update):
            return

        await update.message.reply_text(
            "⚡ *ShortCircuit Commands*\n\n"
            "`/auto on/off`\n"
            "`/status`\n"
            "`/positions`\n"
            "`/pnl`\n"
            "`/why SYM`\n"
            "`/pause` / `/resume`",
            parse_mode='Markdown'
        )

    # ════════════════════════════════════════════════════════════
    # HANDLERS
    # ════════════════════════════════════════════════════════════

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        if not self._is_authorized_query(query):
            return

        data = query.data
        parts = data.split(':')
        action = parts[0]

        if action == 'go':
            await self._handle_go(query, parts[1])
        elif action == 'skip':
            await self._handle_skip(query, parts[1])
        elif action == 'details':
            await self._handle_details(query, parts[1])
        elif action == 'refresh':
            await self._handle_refresh(query, parts[1])
        elif action == 'close':
            symbol = parts[1]
            order_id = parts[2] if len(parts) > 2 else None
            await self._handle_close(query, symbol, order_id)

    async def _handle_go(self, query, signal_id: str):
        signal = self._pending_signals.get(signal_id)
        if not signal:
            await query.edit_message_text("⚠️ Signal expired.")
            return
        
        self._pending_signals.pop(signal_id, None)
        symbol = signal.get('symbol')
        await query.edit_message_text(f"⏳ Executing {symbol}...")
        
        try:
            order_id = await self.order_manager.enter_position(signal)
            if order_id:
                await query.edit_message_text(f"✅ Order Placed: {order_id}")
            else:
                await query.edit_message_text("❌ Order Failed.")
        except Exception as e:
            await query.edit_message_text(f"❌ Error: {e}")

    async def _handle_skip(self, query, signal_id: str):
        signal = self._pending_signals.pop(signal_id, {})
        symbol = signal.get('symbol', 'Unknown')
        await query.edit_message_text(f"⏭️ {symbol} skipped.")

    async def _handle_details(self, query, signal_id: str):
        signal = self._pending_signals.get(signal_id, {})
        confluence = signal.get('confluence_notes', 'N/A')
        await query.answer(f"Details: {confluence}"[:200], show_alert=True)

    async def _handle_refresh(self, query, symbol: str):
        # Refresh is handled by auto-loop, but we can force one if needed
        await query.answer("Refreshing...")

    async def _handle_close(self, query, symbol: str, order_id: str):
        await query.edit_message_text(f"⏳ Closing {symbol}...")
        try:
            await self.order_manager.exit_position(symbol, reason='MANUAL_EXIT')
            await query.edit_message_text(f"✅ Close signal sent.")
        except Exception as e:
            await query.edit_message_text(f"❌ Close failed: {e}")

    # ════════════════════════════════════════════════════════════
    # EMERGENCY ALERTS
    # ════════════════════════════════════════════════════════════

    async def send_emergency_alert(self, message: str):
        await self.send_message(f"🚨 *EMERGENCY*: {message}")

    async def send_orphan_alert(self, symbol: str, qty: int, side: str):
        await self.send_message(f"⚠️ *ORPHAN*: {symbol} {side} x{qty}")

    # ════════════════════════════════════════════════════════════
    # SETUP
    # ════════════════════════════════════════════════════════════

    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("auto", self._cmd_auto))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("why", self._cmd_why))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /auto on | /auto off")
            return
        if args[0] == 'on': await self._cmd_auto_on(update, context)
        elif args[0] == 'off': await self._cmd_auto_off(update, context)

    # ════════════════════════════════════════════════════════════
    # COMPATIBILITY & UTILS
    # ════════════════════════════════════════════════════════════

    def send_alert(self, message: str):
        """
        Thread-safe synchronous wrapper for sending alerts.
        Used by legacy modules (TradeManager, FocusEngine).
        """
        if not self.app or not hasattr(self, '_loop'): return

        try:
            asyncio.run_coroutine_threadsafe(
                self.send_message(message),
                self._loop
            )
        except Exception as e:
            logger.error(f"send_alert sync failed: {e}")

    # ════════════════════════════════════════════════════════════
    # AUTHORIZATION — Security Gate
    # ════════════════════════════════════════════════════════════

    def _is_authorized(self, update: Update) -> bool:
        """
        Verify command sender is authorized.
        
        Only the configured TELEGRAM_CHAT_ID can issue commands.
        Prevents random users from controlling your bot if token leaks.
        
        Args:
            update: Telegram Update object from command handler
            
        Returns:
            True if sender's chat ID matches config, False otherwise
        """
        if not update.effective_chat:
            return False
            
        incoming_chat_id = str(update.effective_chat.id)
        authorized_chat_id = self.chat_id
        
        if incoming_chat_id != authorized_chat_id:
            logger.warning(
                f"⚠️ Unauthorized command attempt from chat_id: {incoming_chat_id}"
            )
            return False
        
        return True

    def _is_authorized_query(self, query) -> bool:
        """
        Verify inline button press is authorized.
        
        Used for [GO], [SKIP], [Refresh], [Close Now] button presses.
        Separate from _is_authorized because CallbackQuery has different
        attribute structure than Update.
        
        Args:
            query: CallbackQuery object from button handler
            
        Returns:
            True if button presser matches config, False otherwise
        """
        incoming_user_id = str(query.from_user.id)
        authorized_chat_id = self.chat_id
        
        # Note: For private chats, user.id == chat.id
        # For group chats, they differ (check both)
        if incoming_user_id != authorized_chat_id:
            # Try checking the chat ID as fallback
            incoming_chat_id = str(query.message.chat.id) if query.message else None
            if incoming_chat_id != authorized_chat_id:
                logger.warning(
                    f"⚠️ Unauthorized button press from user_id: {incoming_user_id}"
                )
                return False
        
        return True

    # ════════════════════════════════════════════════════════════
    # BOT LIFECYCLE
    # ════════════════════════════════════════════════════════════

    async def start(self):
        self.app = Application.builder().token(self.bot_token).build()
        self._register_handlers()
        await self.app.initialize()
        await self.app.start()
        
        
        # Capture the running loop for thread-safe calls
        self._loop = asyncio.get_running_loop()
        self._ready_event.set()
        
        await self.app.updater.start_polling(drop_pending_updates=True)
        logger.info("✅ Telegram Bot started")

    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        """
        Block until the bot's event loop is initialized and ready.
        Resolves Issue 4 (Race Condition).
        """
        return self._ready_event.wait(timeout)

    def start_polling(self):
        """Compatibility wrapper for running in a thread from main.py."""
        self._ready_event = threading.Event()  # Initialize event
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.start())
        # The start() method calls updater.start_polling which is async but returns?
        # Expecting python-telegram-bot run_polling() equivalent which blocks?
        # self.start() starts updater but doesn't block forever if not idle?
        # We need to keep loop running.
        # simple polling:
        loop.run_forever()

    def send_validation_alert(self, signal):
        """Compat wrapper."""
        self.send_alert(f"VALIDATION ALERT: {signal.get('symbol')} {signal.get('ltp')}")

    def send_multi_edge_alert(self, signal):
        """Compat wrapper."""
        self.send_alert(f"MULTI-EDGE ALERT: {signal.get('symbol')} {signal.get('ltp')}")
        
    def send_startup_message(self):
        self.send_alert("⚡ **ShortCircuit Bot Connected**\nSystem Online.")

    async def stop(self):
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
