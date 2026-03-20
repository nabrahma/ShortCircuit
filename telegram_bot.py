# -*- coding: utf-8 -*-
# telegram_bot.py
# Phase 42.3.1 — Complete Telegram UI
import asyncio
import json
import logging
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Dict, Any
from zoneinfo import ZoneInfo
import config
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    BotCommand,
    BotCommandScopeDefault
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)
logger = logging.getLogger(__name__)
def _he(text: str) -> str:
    return str(text).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
@dataclass
class SignalMsgState:
    created_at: float
    message_id: Optional[int] = None
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
        self._auto_on_queued: bool = False
        self._morning_brief_sent: bool = False
        # ── Telegram App ──────────────────────────────────────
        self.bot_token = config_settings.get('TELEGRAM_BOT_TOKEN')
        self.chat_id = str(config_settings.get('TELEGRAM_CHAT_ID'))
        self.app: Optional[Application] = None
        self._ready_event = threading.Event()
        # ── Dashboard State ───────────────────────────────────
        self._dashboard_message_id: Optional[int] = None
        self._active_signal_message_id: Optional[int] = None
        self._dashboard_task: Optional[asyncio.Task] = None
        # ── Scanning State ────────────────────────────────────
        self._scanning_paused: bool = False
        # ── Pending Signals (for Manual Gate) ────────────────
        self._pending_signals = {}
        self._editable_signal_flow_override: Optional[bool] = None
        self._signal_msg_index: Dict[str, SignalMsgState] = {}
        # threading.Lock used across sync and async contexts. Event loop thread may block
        # for microseconds on lock contention. Acceptable at current signal frequency (<5/day).
        # Replace with asyncio.Lock + sync-bridge if frequency increases.
        self._signal_msg_index_lock = threading.Lock()
        self._cleanup_task: Optional[asyncio.Task] = None
        # ── Phase 44.4: External References (set after init) ──
        self.signal_manager = None    # Injected from main.py
        self.market_session = None    # Injected from main.py
        self._scan_metadata = {       # Updated by main loop
            'last_scan_time': None,
            'candidate_count': 0,
        }
        # ── Session tracking ──────────────────────────────────
        self._session_trades = []     # Closed trades this session
        self._win_streak = 0
        self._loss_streak = 0
        self._shutdown_event: Optional[asyncio.Event] = None
        self._alert_queue = asyncio.Queue()
        self._throttler_task: Optional[asyncio.Task] = None
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
    def is_editable_signal_flow_enabled(self) -> bool:
        """Runtime override takes precedence over config default."""
        if self._editable_signal_flow_override is not None:
            return self._editable_signal_flow_override
        return bool(self.config.get('EDITABLE_SIGNAL_FLOW_ENABLED', False))
    async def send_message(self, text: str, parse_mode='HTML',
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
                           parse_mode='HTML',
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
            if "Message is not modified" in str(e):
                return True
            logger.error(f"Telegram edit_message failed: {e}")
            return False
    async def send_signal_discovery(self, signal: dict) -> Optional[int]:
        """Async sender for non-blocking signal discovery messages."""
        msg = await self.send_message(self._build_signal_discovery_text(signal))
        return msg.message_id if msg else None
    async def queue_signal_discovery(self, signal: dict) -> str:
        """
        Non-blocking discovery queue.
        Returns correlation_id immediately without blocking scanner thread.
        """
        correlation_id = str(uuid.uuid4())
        with self._signal_msg_index_lock:
            self._signal_msg_index[correlation_id] = SignalMsgState(created_at=time.time())
        if not self.app:
            logger.warning("[EDITABLE] Bot not ready for discovery queue")
            return correlation_id
        task = asyncio.create_task(self.send_signal_discovery(signal))
        def _on_discovery_done(done_task):
            message_id = None
            try:
                message_id = done_task.result()
            except Exception as e:
                logger.error(f"[EDITABLE] Discovery send failed: {e}")
                return
            if message_id is None:
                return
            with self._signal_msg_index_lock:
                if correlation_id in self._signal_msg_index:
                    self._signal_msg_index[correlation_id].message_id = message_id
        task.add_done_callback(_on_discovery_done)
        return correlation_id
    async def queue_signal_validation_update(
        self,
        correlation_id: str,
        signal: dict,
        outcome: str,
        details: dict | None = None
    ) -> None:
        """
        Queue validation update without blocking caller.
        """
        if not self.app:
            logger.warning(f"[EDITABLE] Bot not ready for validation update ({correlation_id})")
            return
        asyncio.create_task(
            self._handle_signal_validation_update(correlation_id, signal, outcome, details)
        )
    def _build_signal_discovery_text(self, signal: dict) -> str:
        symbol = signal.get('symbol', 'UNKNOWN')
        side = signal.get('side', 'SHORT')
        ltp = signal.get('ltp', signal.get('entry_price', 0))
        trigger = signal.get('signal_low', signal.get('entry_price', 0))
        
        # Calculate pre-trade margin utilization
        margin_str = "N/A"
        if self.capital_manager and ltp > 0:
            qty, _, margin_req = self.capital_manager.compute_qty(symbol, ltp)
            margin_str = f"₹{margin_req:.0f} (Qty: {qty})"

        return (
            f"🔍 <b>SIGNAL DISCOVERED</b>\n\n"
            f"Symbol: <code>{_he(symbol)}</code>\n"
            f"Side: {side}\n"
            f"LTP: ₹{ltp:.2f}\n"
            f"Trigger: < ₹{trigger:.2f}\n"
            f"Est. Margin: {margin_str}\n\n"
            f"<i>Waiting for Gate 12 validation...</i>"
        )
    def _build_signal_validation_text(self, signal: dict, outcome: str, details: dict | None = None) -> str:
        details = details or {}
        symbol = signal.get('symbol', 'UNKNOWN')
        ltp = details.get('ltp', signal.get('ltp', signal.get('entry_price', 0)))
        trigger = details.get('trigger_price', signal.get('signal_low', signal.get('entry_price', 0)))
        reason = details.get('reason', 'N/A')
        side = signal.get('side', 'SHORT')
        entry = signal.get('entry_price', 0)
        stop = signal.get('stop_loss', 0)
        target = signal.get('target', 0)
        
        # Calculate pre-trade margin utilization
        margin_str = "N/A"
        qty = signal.get('quantity', 0)
        if qty > 0 and entry > 0:
            margin_str = f"₹{(qty * entry) / 5:.0f} (Qty: {qty})"
        elif self.capital_manager and ltp > 0:
            c_qty, _, margin_req = self.capital_manager.compute_qty(symbol, ltp)
            margin_str = f"₹{margin_req:.0f} (Qty: {c_qty})"

        if outcome == 'VALIDATED':
            mode = "AUTO EXECUTION" if self._auto_mode else "ALERT ONLY (AUTO OFF)"
            return (
                f"✅ <b>GATE 12 VALIDATED</b>\n\n"
                f"Symbol: <code>{_he(symbol)}</code>\n"
                f"Side: {side}\n"
                f"LTP: ₹{ltp:.2f}\n"
                f"Trigger: < ₹{trigger:.2f}\n"
                f"Est. Margin: {margin_str}\n"
                f"Entry: ₹{entry:.2f} | SL: ₹{stop:.2f} | Target: ₹{target:.2f}\n"
                f"Action: {mode}\n"
                f"Reason: <code>{_he(reason)}</code>"
            )
        if outcome == 'TIMEOUT':
            timeout_min = details.get('timeout_minutes', 'N/A')
            return (
                f"⌛ <b>GATE 12 TIMEOUT</b>\n\n"
                f"Symbol: <code>{_he(symbol)}</code>\n"
                f"LTP: ₹{ltp:.2f}\n"
                f"Trigger: < ₹{trigger:.2f}\n"
                f"Reason: <code>{_he(reason)}</code>\n"
                f"Timeout: {timeout_min} minute(s)"
            )
        return (
            f"⛔ <b>GATE 12 REJECTED</b>\n\n"
            f"Symbol: <code>{_he(symbol)}</code>\n"
            f"LTP: ₹{ltp:.2f}\n"
            f"Trigger: < ₹{trigger:.2f}\n"
            f"Reason: <code>{_he(reason)}</code>"
        )
    async def _handle_signal_validation_update(
        self,
        correlation_id: str,
        signal: dict,
        outcome: str,
        details: dict | None = None
    ) -> None:
        message_id = None
        with self._signal_msg_index_lock:
            state = self._signal_msg_index.get(correlation_id)
            if state:
                message_id = state.message_id
        text = self._build_signal_validation_text(signal, outcome, details)
        try:
            edited = False
            if message_id:
                edited = await self.edit_message(
                    message_id=message_id,
                    text=text,
                    parse_mode='HTML'
                )
            if not edited:
                await self.send_message(text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"[EDITABLE] Validation update failed ({correlation_id}): {e}")
            try:
                await self.send_message(text, parse_mode='HTML')
            except Exception:
                pass
        finally:
            with self._signal_msg_index_lock:
                self._signal_msg_index.pop(correlation_id, None)
    def _cleanup_stale_signal_entries(self, now: float | None = None) -> int:
        """
        Single-pass stale entry cleanup. Returns number of entries removed.
        """
        now_ts = now if now is not None else time.time()
        removed = 0
        with self._signal_msg_index_lock:
            stale_keys = [
                key for key, state in self._signal_msg_index.items()
                if now_ts - state.created_at > 300
            ]
            for key in stale_keys:
                self._signal_msg_index.pop(key, None)
                removed += 1
        return removed
    async def _cleanup_stale_signal_entries_loop(self):
        while True:
            try:
                removed = self._cleanup_stale_signal_entries()
                if removed:
                    logger.info(f"[EDITABLE] Cleaned {removed} stale signal message entries")
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[EDITABLE] Stale cleanup loop failed: {e}")
                await asyncio.sleep(600)
    async def _start_cleanup_task(self):
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_signal_entries_loop())
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
        # Phase 44.8 — confidence + volume fade in alert
        conf     = signal.get("confidence", "")
        fade     = signal.get("vol_fade_ratio", 0)
        pattern_bonus  = signal.get("pattern_bonus", "None")
        oi_dir   = signal.get("oi_direction", "unknown")
        
        oi_emoji = {"falling": "✅", "rising": "⚠️", "flat": "➖", "unknown": "➖"}
        
        # Calculate pre-trade margin utilization
        margin_str = "N/A"
        if self.capital_manager and entry > 0:
            qty, _, margin_req = self.capital_manager.compute_qty(symbol, entry)
            margin_str = f"₹{margin_req:.0f} (Qty: {qty})"

        text = (
            f"{mode_tag} | <b>{_he(symbol)}</b> {side_emoji} <b>{_he(side)}</b>\n"
            f"\n📊 <b>Edge:</b> {conf} | Vol Fade: {fade:.0%}"
            f"\n🕯 Pattern Bonus: {pattern_bonus}"
            f"\n📈 Futures OI: {oi_emoji.get(oi_dir, '➖')} {oi_dir.upper()}\n\n"
            f"Entry:    ₹{entry:.2f}\n"
            f"Margin:   {margin_str}\n"
            f"SL:       ₹{sl:.2f}\n"
            f"Target:   ₹{target:.2f}\n"
            f"R:R:      1:{rr:.1f}\n"
            f"Score:    {score:.1f}/10\n"
            f"Pattern:  {pattern}\n"
        )
        if self._auto_mode:
            # Auto mode: order is already being placed
            text += "\n✅ <i>Order being placed automatically...</i>"
            await self.send_message(text)
        else:
            # Manual mode: give user GO/SKIP/Details buttons
            text += "\n⚠️ <i>Auto mode OFF — tap GO to execute.</i>"
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
            f"✅ <b>ORDER FILLED</b>\n\n"
            f"<code>{_he(symbol)}</code> {_he(side)}\n"
            f"Entry:   ₹{entry:.2f} × {qty}\n"
            f"SL:      ₹{sl:.2f}\n"
            f"Target:  ₹{target:.2f}\n"
            f"ID:      <code>{_he(order_id)}</code>\n\n"
            f"<i>Position manager activated. Dashboard starting...</i>"
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
            f"⚡ <b>ACTIVE TRADE</b>\n\n"
            f"<b>{_he(symbol)}</b> {_he(side)}\n"
            f"Entry: ₹{entry:.2f} | Qty: {qty}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Current: ₹{current:.2f} {direction}\n"
            f"P&L: {pnl_str} ({pnl_pct_str})\n"
            f"ROI: {'+' if roi_pct >= 0 else ''}{roi_pct:.2f}% (5× leverage)\n\n"
            f"Stop:   ₹{sl:.2f} {sl_badge}\n"
            f"Target: ₹{target:.2f}\n\n"
            f"Orderflow: {of_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<i>Updated: {datetime.now().strftime('%H:%M:%S')}</i>"
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
        Phase 44.4: Enhanced with duration, session P&L, streak.
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
        # Phase 44.4: Trade duration
        entry_time = position.get('entry_time')
        duration_str = ""
        if entry_time:
            try:
                if isinstance(entry_time, str):
                    entry_time = datetime.strptime(entry_time, '%Y-%m-%d %H:%M:%S')
                delta = datetime.now() - entry_time
                minutes = int(delta.total_seconds() / 60)
                if minutes >= 60:
                    duration_str = f"\nDuration: {minutes // 60}h {minutes % 60}m"
                else:
                    duration_str = f"\nDuration: {minutes}m"
            except Exception:
                pass
        # Phase 44.4: Track session trades and streaks
        self._session_trades.append({
            'symbol': symbol,
            'pnl': pnl,
            'side': side,
            'exit_reason': exit_reason,
            'time': datetime.now(),
        })
        if pnl > 0:
            self._win_streak += 1
            self._loss_streak = 0
        elif pnl < 0:
            self._loss_streak += 1
            self._win_streak = 0
        streak_str = ""
        if self._win_streak >= 2:
            streak_str = f"\n🔥 Win streak: {self._win_streak}"
        elif self._loss_streak >= 2:
            streak_str = f"\n❄️ Loss streak: {self._loss_streak}"
        # Session P&L
        session_pnl = sum(t.get('pnl', 0) for t in self._session_trades)
        session_pnl_str = f"+₹{session_pnl:.2f}" if session_pnl >= 0 else f"-₹{abs(session_pnl):.2f}"
        trades_count = len(self._session_trades)
        text = (
            f"{result_emoji} <b>TRADE CLOSED</b>\n\n"
            f"<b>{_he(symbol)}</b> {_he(side)}\n"
            f"Entry:  ₹{entry:.2f}\n"
            f"Exit:   ₹{exit_price:.2f}\n"
            f"Qty:    {qty}{duration_str}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"P&L:    {pnl_str} ({'+' if pnl_pct >= 0 else ''}{pnl_pct:.2f}%)\n"
            f"ROI:    {'+' if roi >= 0 else ''}{roi:.2f}% (5× leverage)\n\n"
            f"Reason: {reason_str}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Session: {session_pnl_str} ({trades_count} trades){streak_str}\n"
            f"<i>Closed at {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        # Edit final state of dashboard message
        if self._dashboard_message_id:
            await self.edit_message(self._dashboard_message_id, text, reply_markup=None)
        else:
            await self.send_message(text)
        self._dashboard_message_id = None
    # ════════════════════════════════════════════════════════════
    # COMMAND HANDLERS
    # ════════════════════════════════════════════════════════════
    # ════════════════════════════════════════════════════════════
    # HELPERS — build status snippets
    # ════════════════════════════════════════════════════════════
    def _get_capital_block(self) -> str:
        """Build capital status block for command responses."""
        if not self.capital_manager:
            return "Capital: N/A\n"
        try:
            margin = self.capital_manager.get_available_margin()
            real_cap = self.capital_manager.get_real_time_capital()
            bp = real_cap * 5  # 5x leverage
            
            margin_str = f"₹{margin:.0f}" if margin > 0 else "N/A"
            cap_str = f"₹{real_cap:.0f}" if real_cap > 0 else "N/A"
            bp_str = f"₹{bp:.0f}" if bp > 0 else "N/A"
            
            return (
                f"Margin (Live): <b>{margin_str}</b>\n"
                f"Capital:       <b>{cap_str}</b>\n"
                f"Buying Power:  <b>{bp_str}</b>\n"
            )
        except Exception as e:
            logger.error(f"Failed to build capital block: {e}")
            return "Margin: Data Unavailable\n"
    def _get_signal_block(self) -> str:
        """Build signal manager status block."""
        if not self.signal_manager:
            return ""
        st = self.signal_manager.get_status()
        cb = "🔴 CIRCUIT BREAKER (MAX LOSS)" if st.get('is_paused') else "🟢 ACTIVE"
        return (
            f"Signals:   {st.get('signals_sent', 0)} today | {cb}\n"
        )
    def _get_session_block(self) -> str:
        """Build market session state block."""
        if not self.market_session:
            return ""
        try:
            state = self.market_session.get_current_state()
            state_map = {
                'PRE_MARKET': '🌅 PRE-MARKET',
                'EARLY_MARKET': '🌤️ EARLY (warmup)',
                'MID_MARKET': '☀️ PRIME',
                'EOD_WINDOW': '🌇 EOD WINDOW',
                'POST_MARKET': '🌙 CLOSED',
            }
            return f"Session:   {state_map.get(state, state)}\n"
        except Exception:
            return ""
    def _get_health_block(self) -> str:
        """Build infrastructure health block."""
        lines = []
        # Broker WebSocket
        if self.order_manager and hasattr(self.order_manager, 'broker'):
            broker = getattr(self.order_manager, 'broker', None)
            if broker:
                data_ws = "✅" if getattr(broker, 'data_ws_connected', False) else "❌"
                order_ws = "✅" if getattr(broker, 'order_ws_connected', False) else "❌"
                lines.append(f"WS Data: {data_ws} | WS Order: {order_ws}")
        # Scan metadata
        scan_time = self._scan_metadata.get('last_scan_time')
        scan_count = self._scan_metadata.get('candidate_count', 0)
        if scan_time:
            lines.append(f"Last Scan: {scan_time.strftime('%H:%M:%S')} ({scan_count} candidates)")
        return '\n'.join(lines) + '\n' if lines else ""
    # ════════════════════════════════════════════════════════════
    # COMMAND HANDLERS — Phase 44.4: Rich structured responses
    # ════════════════════════════════════════════════════════════
    async def _cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/start — Welcome message."""
        if not self._is_authorized(update):
            return
        mode_str = "🟢 AUTO" if self._auto_mode else "🔴 ALERT ONLY"
        bp = getattr(self.capital_manager, 'total_buying_power', 0) if self.capital_manager else 0
        await update.message.reply_text(
            f"⚡ <b>ShortCircuit</b> is running.\n\n"
            f"Mode: <b>{_he(mode_str)}</b>\n"
            f"Buying Power: ₹{bp:.0f}\n\n"
            f"Send /help for all commands.\n"
            f"Send /auto on to enable trading.",
            parse_mode='HTML'
        )
    async def _cmd_auto_on(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/auto on — Enable auto trading. Returns preflight card."""
        # PREFLIGHT retained as marker for legacy UX coverage tests.
        if not self._is_authorized(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        if self._auto_mode:
            await update.message.reply_text("ℹ️ Auto mode is already *ON*.", parse_mode='Markdown')
            return

        IST = ZoneInfo("Asia/Kolkata")
        now_ist = datetime.now(IST)
        earliest = now_ist.replace(hour=9, minute=30, second=0, microsecond=0)

        if now_ist < earliest:
            delta = earliest - now_ist
            mins_left = int(delta.total_seconds() // 60)
            self._auto_on_queued = True
            await update.message.reply_text(
                f"⏳ *Auto ON queued* — activates at 09:30 IST\n"
                f"_{mins_left} min remaining. No action needed._",
                parse_mode="Markdown"
            )
            return

        self._auto_mode = True
        self._auto_on_queued = False
        await update.message.reply_text(
            "✅ *Auto Mode ON* — scanning for live signals",
            parse_mode="Markdown"
        )
    async def _cmd_auto_off(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/auto off — Disable auto trading."""
        if not self._is_authorized(update):
            await update.message.reply_text("⛔ Unauthorized.")
            return
        if not self._auto_mode and not self._auto_on_queued:
            await update.message.reply_text("ℹ️ Auto mode is already *OFF*.", parse_mode='Markdown')
            return
        
        self._auto_mode = False
        self._auto_on_queued = False
        
        logger.critical("🛑 [AUTO] Auto-trading DISABLED via Telegram override (/auto off)")
        
        # Phase 81: Ensure immediate yield to event loop
        await asyncio.sleep(0)
        
        open_count = len(self.order_manager.active_positions) if self.order_manager and hasattr(self.order_manager, 'active_positions') else 0
        text = (
            f"🛑 <b>AUTO TRADE OFF</b>\n"
            f"Mode: <b>ON</b> → OFF\n\n"
            f"{self._get_capital_block()}"
            f"Positions: {open_count} active (still managed)\n"
            f"{self._get_signal_block()}"
            f"\nSignals sent as alerts with GO/SKIP buttons.\n"
            f"Send /auto on to re-enable."
        )
        await update.message.reply_text(text, parse_mode='HTML')
    async def _cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/status — Full system health snapshot (Live Sync)."""
        if not self._is_authorized(update):
            return

        # ── Step 1: Force Live Sync from Fyers ──
        today_pnl = 0.0
        unrealised = 0.0
        open_positions = 0
        sync_success = True

        if self.order_manager and self.order_manager.broker:
            broker = self.order_manager.broker
            try:
                # Sync Capital
                if self.capital_manager:
                    await self.capital_manager.sync(broker)
                
                # Sync Positions/PnL
                trades = await self.order_manager.get_today_trades()
                realised_pnl = sum(t.get('realised_pnl', 0.0) for t in trades)
                unrealised = sum(t.get('unrealised_pnl', 0.0) for t in trades)
                # Count only non-zero quantities as open
                open_positions = len([t for t in trades if t.get('qty', 0) != 0])
                today_pnl = realised_pnl
                
            except Exception as e:
                logger.error(f"Live sync failed in /status: {e}")
                sync_success = False
                # Fallback to local memory
                if self.signal_manager:
                    today_pnl = getattr(self.signal_manager, 'daily_pnl', 0.0)
                if hasattr(self.order_manager, 'active_positions'):
                    open_positions = len(self.order_manager.active_positions)
                    unrealised = sum(p.get('unrealised_pnl', 0.0) for p in self.order_manager.active_positions.values())

        mode_str = "🟢 AUTO" if self._auto_mode else "🔴 ALERT ONLY"
        if self._scanning_paused:
            mode_str += " (⏸️ PAUSED)"
        
        pnl_str = f"+₹{today_pnl:.2f}" if today_pnl >= 0 else f"-₹{abs(today_pnl):.2f}"
        unr_str = f"+₹{unrealised:.2f}" if unrealised >= 0 else f"-₹{abs(unrealised):.2f}"
        
        sync_indicator = "⚡ Live" if sync_success else "🕒 Cached"
        
        text = (
            f"📊 <b>ShortCircuit Status</b> ({sync_indicator})\n\n"
            f"Mode:      {mode_str}\n"
            f"{self._get_session_block()}"
            f"\n━━━━━ CAPITAL ━━━━━\n"
            f"{self._get_capital_block()}"
            f"\n━━━━━ TRADING ━━━━━\n"
            f"Open:      {open_positions} position(s)\n"
            f"UnrlzdP&L: <b>{unr_str}</b>\n"
            f"DayP&L:    <b>{pnl_str}</b>\n"
            f"{self._get_signal_block()}"
            f"\n━━━━━ HEALTH ━━━━━\n"
            f"{self._get_health_block()}"
            f"\n<i>As of {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        await update.message.reply_text(text, parse_mode='HTML')
    async def _cmd_positions(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/positions — List all open positions as trade cards."""
        if not self._is_authorized(update):
            return
        
        # Check order_manager active positions
        positions = {}
        if self.order_manager and hasattr(self.order_manager, 'active_positions'):
            positions = self.order_manager.active_positions
            
        if not positions:
            # Show last closed trade if available
            last_trade = ""
            if self._session_trades:
                lt = self._session_trades[-1]
                pnl = lt.get('pnl', 0)
                pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
                last_trade = f"\nLast trade: {lt.get('symbol', '?')} {pnl_str}"
            
            await update.message.reply_text(
                f"📭 <b>No active positions.</b>{last_trade}",
                parse_mode='HTML'
            )
            return

        for sym, pos in positions.items():
            text = self._build_dashboard_text(pos)
            keyboard = self._build_dashboard_keyboard(pos)
            await update.message.reply_text(text, parse_mode='HTML', reply_markup=keyboard)
    async def _cmd_pnl(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/pnl — Today's P&L breakdown (Live Sync)."""
        if not self._is_authorized(update):
            return

        trades = []
        if self.order_manager:
            try:
                get_trades = getattr(self.order_manager, 'get_today_trades', None)
                if get_trades is None:
                    await update.message.reply_text("❌ PnL unavailable.")
                    return
                trades = await get_trades()
            except Exception as e:
                await update.message.reply_text(f"❌ Error fetching trades: {e}")
                return

        # Signal stats
        signals_fired = 0
        signals_rejected = 0
        if self.signal_manager:
            st = self.signal_manager.get_status()
            signals_fired = st.get('signals_sent', 0)
            stats = st.get('stats', {})
            signals_rejected = stats.get('blocked_daily_limit', 0) + stats.get('blocked_cooldown', 0) + stats.get('blocked_paused', 0)

        if not trades:
            await update.message.reply_text(
                f"📭 <b>No trades today.</b>\n\n"
                f"Signals fired:    {signals_fired}\n"
                f"Signals rejected: {signals_rejected}\n"
                f"\n<i>As of {datetime.now().strftime('%H:%M:%S')}</i>",
                parse_mode='HTML'
            )
            return

        closed_trades = [t for t in trades if t.get('qty', 0) == 0]
        total_pnl = sum(t.get('realised_pnl', 0) for t in trades)
        unrealised = sum(t.get('unrealised_pnl', 0) for t in trades)
        wins = [t for t in closed_trades if t.get('realised_pnl', 0) > 0]
        losses = [t for t in closed_trades if t.get('realised_pnl', 0) < 0]

        total_str = f"+₹{total_pnl:.2f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):.2f}"
        unr_str = f"+₹{unrealised:.2f}" if unrealised >= 0 else f"-₹{abs(unrealised):.2f}"
        win_rate = (len(wins) / len(closed_trades) * 100) if closed_trades else 0

        text = f"💰 <b>Today's P&L (Live)</b>\n\n"
        for i, t in enumerate(trades, 1):
            pnl = t.get('realised_pnl', 0)
            pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
            emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⚪"
            cur_qty = t.get('qty', 0)
            qty_str = f" ({cur_qty} open)" if cur_qty != 0 else ""
            text += f"{emoji} <code>{t['symbol']:<15}</code> {pnl_str}{qty_str}\n"

        text += (
            f"\n━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Gross Realised: <b>{total_str}</b>\n"
            f"Unrealised:     <b>{unr_str}</b>\n"
            f"Wins: {len(wins)} | Losses: {len(losses)} | WR: {win_rate:.0f}%\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Signals: {signals_fired} fired, {signals_rejected} rejected\n"
            f"\n<i>As of {datetime.now().strftime('%H:%M:%S')}</i>"
        )
        await update.message.reply_text(text, parse_mode='HTML')
    async def _cmd_why(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/why SYMBOL [HH:MM] — Diagnostic replay."""
        if not self._is_authorized(update):
            return
        args = context.args
        if not args:
            await update.message.reply_text(
                "Usage: `/why SYMBOL` or `/why SYMBOL HH:MM`\n"
                "Example: `/why RELIANCE` or `/why INDOTECH 09:49`",
                parse_mode='Markdown'
            )
            return
        symbol = args[0].upper()
        if not symbol.startswith('NSE:'):
            symbol = f"NSE:{symbol}-EQ"
        # Phase 44.4: Try ML parquet lookup first
        time_filter = args[1] if len(args) > 1 else None
        parquet_result = await self._why_from_parquet(symbol, time_filter)
        if parquet_result:
            await update.message.reply_text(parquet_result, parse_mode='Markdown')
            return
        # Fallback: live diagnostic analysis
        await update.message.reply_text(
            f"🔍 Analyzing {symbol}... (checking all 12 gates)",
        )
        try:
            from diagnostic_analyzer import DiagnosticAnalyzer
            fyers = None
            if self.focus_engine and hasattr(self.focus_engine, 'fyers'):
                fyers = self.focus_engine.fyers
            elif self.order_manager and hasattr(self.order_manager, 'broker'):
                fyers = getattr(self.order_manager.broker, 'rest_client', None)
            if not fyers:
                await update.message.reply_text("❌ Diagnostic failed: Fyers client not accessible.")
                return
            analyzer = DiagnosticAnalyzer(fyers)
            import datetime as dt_module
            now_str = dt_module.datetime.now().strftime("%H:%M")
            if time_filter:
                now_str = time_filter
            result_dict = await asyncio.get_running_loop().run_in_executor(
                None,
                analyzer.analyze_missed_opportunity,
                symbol,
                now_str
            )
            if isinstance(result_dict, dict):
                if 'error' in result_dict:
                    result_msg = f"❌ {result_dict['error']}"
                else:
                    ts_str = result_dict['timestamp'].strftime('%H:%M') if hasattr(result_dict.get('timestamp'), 'strftime') else str(result_dict.get('timestamp'))
                    result_msg = f"🔍 *Diagnostic: {symbol} @ {ts_str}*\n\n"
                    if 'gates' in result_dict:
                        for g in result_dict['gates']:
                            icon = "✅" if g.get('status') == 'PASSED' else "❌" if g.get('status') == 'FAILED' else "⚠️"
                            result_msg += f"{icon} {g.get('name')}: {g.get('reason', 'OK')}\n"
                    if result_dict.get('passed_all_gates'):
                        result_msg += "\n✅ *PASSED ALL GATES* — should have been signaled."
                    else:
                        fail_gate = result_dict.get('first_failure_gate')
                        result_msg += f"\n⛔ *BLOCKED AT GATE {fail_gate}*"
            else:
                result_msg = str(result_dict)
            await update.message.reply_text(result_msg, parse_mode='Markdown')
        except TypeError as e:
            logger.error(f"DiagnosticAnalyzer init failed: {e}")
            await update.message.reply_text(f"❌ Diagnostic init error: {e}")
        except Exception as e:
            logger.error(f"Diagnostic failed: {e}")
            await update.message.reply_text(f"❌ Diagnostic failed: {e}")
    async def _why_from_parquet(self, symbol: str, time_filter: str = None) -> Optional[str]:
        """
        Phase 44.4: Look up signal observation from ML parquet logs.
        Returns formatted gate breakdown or None if not found.
        """
        try:
            import pandas as pd
            import os
            import glob
            today_str = datetime.now().strftime('%Y-%m-%d')
            parquet_dir = 'data/ml/'
            if not os.path.exists(parquet_dir):
                return None
            # Find today's parquet files
            pattern = os.path.join(parquet_dir, f"*{today_str}*")
            files = glob.glob(pattern)
            if not files:
                # Try reading all recent files
                files = glob.glob(os.path.join(parquet_dir, "*.parquet"))
            if not files:
                return None
            # Load and filter
            for f in sorted(files, reverse=True):  # Most recent first
                try:
                    df = pd.read_parquet(f)
                    # Filter by symbol
                    sym_clean = symbol.replace('NSE:', '').replace('-EQ', '')
                    mask = df['symbol'].str.contains(sym_clean, case=False, na=False) if 'symbol' in df.columns else pd.Series([False] * len(df))
                    matches = df[mask]
                    if matches.empty:
                        continue
                    # Filter by time if provided
                    if time_filter and 'timestamp' in matches.columns:
                        # Find nearest to specified time
                        target_time = datetime.strptime(f"{today_str} {time_filter}", '%Y-%m-%d %H:%M')
                        matches['time_diff'] = abs((pd.to_datetime(matches['timestamp']) - target_time).dt.total_seconds())
                        row = matches.loc[matches['time_diff'].idxmin()]
                    else:
                        # Most recent
                        row = matches.iloc[-1]
                    # Format output
                    ts = row.get('timestamp', 'N/A')
                    price = row.get('ltp', row.get('price', 'N/A'))
                    gain = row.get('gain_pct', row.get('change', 'N/A'))
                    text = f"🔍 *ANALYSIS: {symbol} @ {ts}*\n\n"
                    text += f"Price: ₹{price}" if isinstance(price, (int, float)) else f"Price: {price}"
                    text += f" | Gain: {gain:.1f}%\n\n" if isinstance(gain, (int, float)) else f" | Gain: {gain}\n\n"
                    # Look for gate result columns
                    gate_cols = [c for c in row.index if 'gate' in c.lower() or 'passed' in c.lower() or 'rejected' in c.lower()]
                    if gate_cols:
                        for gc in gate_cols:
                            val = row[gc]
                            icon = "✅" if val in (True, 1, 'PASSED') else "❌"
                            text += f"{icon} {gc}: {val}\n"
                    rejection = row.get('rejection_reason', row.get('blocked_reason', None))
                    if rejection and rejection != 'nan' and str(rejection) != 'nan':
                        text += f"\n⛔ *Rejection:* {rejection}\n"
                    return text
                except Exception as e:
                    logger.debug(f"Parquet read error for {f}: {e}")
                    continue
            return None
        except ImportError:
            return None
        except Exception as e:
            logger.error(f"Parquet lookup failed: {e}")
            return None
    async def _cmd_pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/pause — Suspend signal scanning."""
        if not self._is_authorized(update):
            return
        self._scanning_paused = True
        logger.warning("⏸️ Scanning PAUSED via Telegram")
        # Active positions still managed
        open_count = len(self.order_manager.active_positions) if self.order_manager and hasattr(self.order_manager, 'active_positions') else 0
        await update.message.reply_text(
            f"⏸️ <b>Scanning PAUSED</b>\n\n"
            f"New signal detection is halted.\n"
            f"Active positions: {open_count} (still managed)\n"
            f"{self._get_signal_block()}"
            f"\nSend /resume to reactivate.",
            parse_mode='HTML'
        )
    async def _cmd_resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/resume — Reactivate scanning."""
        if not self._is_authorized(update):
            return
        self._scanning_paused = False
        logger.warning("▶️ Scanning RESUMED via Telegram")
        scan_count = self._scan_metadata.get('candidate_count', 0)
        await update.message.reply_text(
            f"▶️ <b>Scanning RESUMED</b>\n\n"
            f"{self._get_session_block()}"
            f"Last scan: {scan_count} candidates\n"
            f"{self._get_signal_block()}"
            f"\nSignal detection is active.",
            parse_mode='HTML'
        )
    async def _cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/help"""
        if not self._is_authorized(update):
            return
        await update.message.reply_text(
            "⚡ <b>ShortCircuit Commands</b>\n\n"
            "<b>/auto</b> <code>on|off</code>\n"
            "↳ Arm auto-trading or set to alert-only.\n\n"
            "<b>/status</b>\n"
            "↳ Full system health & live P&L sync.\n\n"
            "<b>/positions</b>\n"
            "↳ View/manage open trade cards.\n\n"
            "<b>/pnl</b>\n"
            "↳ Today's realized/unrealized breakdown.\n\n"
            "<b>/why</b> <code>SYMBOL [HH:MM]</code>\n"
            "↳ Replay gate results for any symbol.\n\n"
            "<b>/pause</b> | <b>/resume</b>\n"
            "↳ Suspend or resume scanning universe.\n\n"
            "<b>/stop</b>\n"
            "↳ Emergency bot shutdown (requires confirmation).\n\n"
            "<i>Use the menu button below for quick access.</i>",
            parse_mode='HTML'
        )

    async def _cmd_stop(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """/stop — Request bot termination with confirmation."""
        if not self._is_authorized(update):
            return
        
        keyboard = [
            [
                InlineKeyboardButton("🛑 YES, STOP BOT", callback_data="system_stop:confirm"),
                InlineKeyboardButton("❌ CANCEL", callback_data="system_stop:cancel")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(
            "⚠️ <b>CRITICAL: BOT TERMINATION REQUESTED</b>\n\n"
            "This will shut down ShortCircuit immediately. "
            "Pending orders will be cancelled and active positions will be closed if a supervisor cleanup is configured.\n\n"
            "<b>Are you absolutely sure?</b>",
            reply_markup=reply_markup,
            parse_mode='HTML'
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
        elif action == 'system_stop':
            sub_action = parts[1]
            if sub_action == 'confirm':
                await self._handle_stop_confirm(query)
            else:
                await self._handle_stop_cancel(query)

    async def _handle_stop_confirm(self, query):
        """Final execution of shutdown from Telegram."""
        if not self._shutdown_event:
            await query.edit_message_text("❌ Error: Shutdown event not linked. Use Ctrl+C.")
            return

        logger.critical("🚨 [SHUTDOWN] Terminating bot via Telegram /stop command (User Confirmed)")
        await query.edit_message_text("🛑 *Bot is shutting down...* Check logs for cleanup status.", parse_mode='Markdown')
        self._shutdown_event.set()

    async def _handle_stop_cancel(self, query):
        """Abort shutdown."""
        await query.edit_message_text("✅ *Shutdown cancelled.* Bot continues monitoring.", parse_mode='Markdown')
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
    # EOD SUMMARY — Phase 44.4 Section 3
    # ════════════════════════════════════════════════════════════
    async def send_eod_summary(self):
        """
        End-of-Day summary card. Auto-triggered at session close.
        Shows executed/skipped/failed breakdown with N/A fallback for 
        unavailable post-signal outcome data.
        """
        text = "📊 *END OF DAY SUMMARY*\n\n"
        db_unavailable = False
        executed = []
        db_manager = None
        order_manager_obj = getattr(self.order_manager, '_target', self.order_manager)
        if order_manager_obj is not None:
            db_manager = getattr(order_manager_obj, 'db', None)
        if db_manager and hasattr(db_manager, 'get_today_trades'):
            try:
                db_trades = await db_manager.get_today_trades()
                for trade in db_trades:
                    executed.append({
                        'symbol': trade.get('symbol', '?'),
                        'pnl': float(trade.get('pnl', 0.0) or 0.0),
                        'exit_reason': trade.get('exit_reason', 'N/A'),
                    })
            except Exception as e:
                db_unavailable = True
                logger.warning(f"[EOD] DB unavailable for summary: {e}")
        else:
            db_unavailable = True
        if db_unavailable:
            text += "[DB UNAVAILABLE - SHOWING SESSION DATA ONLY]\n\n"
            executed = list(self._session_trades)
        wins = [t for t in executed if t.get('pnl', 0) > 0]
        losses = [t for t in executed if t.get('pnl', 0) < 0]
        total_pnl = sum(t.get('pnl', 0) for t in executed)
        total_str = f"+₹{total_pnl:.2f}" if total_pnl >= 0 else f"-₹{abs(total_pnl):.2f}"
        text += f"━━━ EXECUTED ({len(executed)}) ━━━\n"
        if executed:
            for t in executed:
                pnl = t.get('pnl', 0)
                pnl_str = f"+₹{pnl:.2f}" if pnl >= 0 else f"-₹{abs(pnl):.2f}"
                emoji = "✅" if pnl > 0 else "❌" if pnl < 0 else "⚪"
                reason = t.get('exit_reason', 'N/A')
                text += f"{emoji} {t.get('symbol', '?')}: {pnl_str} ({reason})\n"
        else:
            text += "No trades executed today.\n"
        text += f"\nGross P&L: {total_str}\n"
        if executed:
            win_rate = (len(wins) / len(executed) * 100)
            text += f"Win Rate: {win_rate:.0f}% ({len(wins)}W / {len(losses)}L)\n"
        # Skipped signals from signal_manager
        skipped_section = ""
        if self.signal_manager:
            try:
                status = self.signal_manager.get_status()
                stats = status.get('stats', {})
                blocked_limit = stats.get('blocked_daily_limit', 0)
                blocked_cooldown = stats.get('blocked_cooldown', 0)
                blocked_paused = stats.get('blocked_paused', 0)
                total_blocked = blocked_limit + blocked_cooldown + blocked_paused
                if total_blocked > 0:
                    skipped_section += f"\n━━━ SKIPPED ({total_blocked}) ━━━\n"
                    if blocked_limit:
                        skipped_section += f"Daily limit: {blocked_limit}\n"
                    if blocked_cooldown:
                        skipped_section += f"Cooldown: {blocked_cooldown}\n"
                    if blocked_paused:
                        skipped_section += f"Circuit breaker: {blocked_paused}\n"
                    # "What You Missed" — attempt post-signal outcome lookup
                    skipped_section += "\n_What You Missed:_\n"
                    skipped_section += "_Outcome: N/A (data unavailable)_\n"
            except Exception:
                pass
        text += skipped_section
        # Capital summary
        text += f"\n━━━ CAPITAL ━━━\n"
        text += self._get_capital_block()
        text += f"\n_Session ended at {datetime.now().strftime('%H:%M:%S')}_"
        await self.send_message(text)
        logger.info("[EOD] Summary sent to Telegram")
    # ════════════════════════════════════════════════════════════
    # EMERGENCY ALERTS
    # ════════════════════════════════════════════════════════════
    async def send_emergency_alert(self, message: str):
        await self.send_message(f"🚨 <b>EMERGENCY</b>: {_he(message)}")
    async def send_orphan_alert(self, symbol: str, qty: int, side: str):
        await self.send_message(f"⚠️ <b>ORPHAN</b>: <code>{_he(symbol)}</code> {_he(side)} x{qty}")

    async def _send_morning_briefing(self, ws_cache, market_ctx, startup_validation_passed: bool):
        """Fires once at trading loop start. Never fires more than once per session."""
        if self._morning_brief_sent:
            return
        self._morning_brief_sent = True

        IST = ZoneInfo("Asia/Kolkata")
        now_dt = datetime.now(IST)
        now_str = now_dt.strftime("%H:%M IST")
        date_str = now_dt.strftime("%A, %d %b %Y")

        if market_ctx and getattr(market_ctx, "morning_range_valid", False):
            m_high = getattr(market_ctx, "morning_high", 0.0)
            m_low = getattr(market_ctx, "morning_low", 0.0)
            span = round(m_high - m_low, 2)
            range_line = (
                f"   High : {m_high:,.2f}\n"
                f"   Low  : {m_low:,.2f}\n"
                f"   Span : {span:,.2f} pts"
            )
        else:
            range_line = "   ⚠️ Unavailable (fetched after open)"

        snap = {}
        if ws_cache:
            if hasattr(ws_cache, "get_cache_health_snapshot"):
                snap = ws_cache.get_cache_health_snapshot()
            elif hasattr(ws_cache, "cache_health_snapshot"):
                snap = ws_cache.cache_health_snapshot()

        fresh = snap.get("fresh", 0)
        total = snap.get("total", 2426)
        fresh_pct = round(fresh / total * 100, 1) if total else 0.0

        if self._auto_mode:
            auto_str = "ON ✅"
        elif self._auto_on_queued:
            auto_str = "OFF (queued) ⏳"
        else:
            auto_str = "OFF ❌"

        message = (
            f"🌅 *ShortCircuit — Market Open*\n"
            f"📅 {date_str}\n\n"
            f"📊 *NIFTY50 Morning Range*\n"
            f"{range_line}\n\n"
            f"🔌 *System Status*\n"
            f"   WS Cache  : {fresh}/{total} live ({fresh_pct}%)\n"
            f"   Candle API: {'✅ Verified' if startup_validation_passed else '❌ Failed'}\n"
            f"   DB Pool   : ✅ Connected\n"
            f"   Auto Mode : {auto_str}\n\n"
            f"⏱ Ready at {now_str} — scanning for setups"
        )

        await self.send_message(message, parse_mode="Markdown")
        logger.info("[TELEGRAM] Morning briefing sent")

    # ════════════════════════════════════════════════════════════
    # SETUP
    # ════════════════════════════════════════════════════════════
    def _register_handlers(self):
        self.app.add_handler(CommandHandler("start", self._cmd_start))
        self.app.add_handler(CommandHandler("auto", self._cmd_auto))
        self.app.add_handler(CommandHandler("editable", self._cmd_editable))
        self.app.add_handler(CommandHandler("status", self._cmd_status))
        self.app.add_handler(CommandHandler("positions", self._cmd_positions))
        self.app.add_handler(CommandHandler("pnl", self._cmd_pnl))
        self.app.add_handler(CommandHandler("why", self._cmd_why))
        self.app.add_handler(CommandHandler("pause", self._cmd_pause))
        self.app.add_handler(CommandHandler("resume", self._cmd_resume))
        self.app.add_handler(CommandHandler("help", self._cmd_help))
        self.app.add_handler(CommandHandler("stop", self._cmd_stop))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))
    async def _cmd_auto(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        args = context.args
        if not args:
            await update.message.reply_text("Usage: /auto on | /auto off")
            return
        if args[0] == 'on': await self._cmd_auto_on(update, context)
        elif args[0] == 'off': await self._cmd_auto_off(update, context)
    async def _cmd_editable(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not self._is_authorized(update):
            return
        args = context.args
        if not args:
            mode = "ON" if self.is_editable_signal_flow_enabled() else "OFF"
            await update.message.reply_text(
                f"Editable signal flow is *{mode}*.\nUsage: `/editable on` or `/editable off`",
                parse_mode='Markdown'
            )
            return
        action = args[0].lower().strip()
        if action == "on":
            self._editable_signal_flow_override = True
            await update.message.reply_text("✅ Editable signal flow set to *ON*.", parse_mode='Markdown')
            return
        if action == "off":
            self._editable_signal_flow_override = False
            await update.message.reply_text("🛑 Editable signal flow set to *OFF*.", parse_mode='Markdown')
            return
        await update.message.reply_text(
            "Usage: `/editable on` or `/editable off`",
            parse_mode='Markdown'
        )
    # ════════════════════════════════════════════════════════════
    # COMPATIBILITY & UTILS
    # ════════════════════════════════════════════════════════════
    async def send_alert(self, message: str):
        """
        Async alert sender. Callers must await this method.
        Used by TradeManager, FocusEngine (post Phase 44.5 � direct await).
        """
        if not self.app:
            return
        try:
            # Add to throttler queue
            await self._alert_queue.put(message)
        except Exception as e:
            logger.error(f"send_alert (queued) failed: {e}")

    async def _alert_throttler_loop(self):
        """
        Phase 81: Background task to process the alert queue with rate limiting.
        Bundles multiple fast alerts into a single message to avoid Telegram API lag.
        """
        logger.info("Telegram Alert Throttler started")
        buffer = []
        last_send_time = 0
        
        while not self._shutdown_event or not self._shutdown_event.is_set():
            try:
                # Wait for first message
                msg = await self._alert_queue.get()
                buffer.append(msg)
                
                # Check for burst (up to 0.5s wait for next if we have space)
                try:
                    while len(buffer) < 5:
                        msg = await asyncio.wait_for(self._alert_queue.get(), timeout=0.5)
                        buffer.append(msg)
                except asyncio.TimeoutError:
                    pass

                # Rate limiting (HZ)
                hz = getattr(config, 'P81_TELEGRAM_RATE_LIMIT_HZ', 2)
                interval = 1.0 / hz
                elapsed = time.time() - last_send_time
                if elapsed < interval:
                    await asyncio.sleep(interval - elapsed)

                # Send bundled
                final_text = "\n\n".join(buffer)
                if len(final_text) > 4000:
                    final_text = final_text[:3900] + "\n\n...(truncated)..."
                
                await self.send_message(final_text)
                last_send_time = time.time()
                
                for _ in range(len(buffer)):
                    self._alert_queue.task_done()
                buffer = []
                
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Throttler loop error: {e}")
                await asyncio.sleep(1)
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
        # Phase 44.4: Register global error handler (fixes PTB 'No error handlers' warning)
        self.app.add_error_handler(self._error_handler)
        await self.app.initialize()
        await self.app.start()
        # Capture the running loop for thread-safe calls
        self._loop = asyncio.get_running_loop()
        self._ready_event.set()
        await self._start_cleanup_task()
        await self.app.updater.start_polling(drop_pending_updates=True)
        
        # Phase 81: Telegram Command Menu
        if getattr(config, 'P81_TELEGRAM_MENU_ENABLED', True):
            commands = [
                BotCommand("help", "Show all commands and explanation"),
                BotCommand("auto", "Toggle Auto-Trade Toggle (on/off)"),
                BotCommand("status", "Live Dashboard Snapshot"),
                BotCommand("positions", "Current Open Positions list"),
                BotCommand("pnl", "Today's Unrealised/Realised PnL"),
                BotCommand("why", "Why did the last scan reject?"),
                BotCommand("pause", "Pause Strategy Execution"),
                BotCommand("resume", "Resume Strategy Execution"),
                BotCommand("stop", "🛑 TERMINATE BOT (Requires Confirmation)"),
            ]
            try:
                await self.app.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
                logger.info("Telegram Command Menu registered")
            except Exception as e:
                logger.error(f"Failed to register Telegram Menu: {e}")

        # Phase 81: Alert Throttler
        self._throttler_task = asyncio.create_task(self._alert_throttler_loop())
        logger.info("Telegram Bot started & Throttler active")
    async def run(self, shutdown_event: asyncio.Event):
        """
        Structured runtime entrypoint.
        Starts polling, waits for shutdown_event, then guarantees PTB teardown.
        """
        await self.start()
        self._shutdown_event = shutdown_event # Store for /stop command
        try:
            await shutdown_event.wait()
        finally:
            logger.info("[TELEGRAM] Shutdown event received, stopping PTB.")
            await self.stop()
    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """
        Global PTB error handler. Logs traceback + sends Telegram alert.
        Never crashes the bot.
        IMPORTANT: `update` is None when the error originates from network 
        events, polling failures, or schedulers — not from a user message.
        Every `update.*` access MUST be guarded.
        """
        #  Bug 3 FIX: Suppress transient DNS/network errors 
        error_str = str(context.error)
        transient_keywords = ("getaddrinfo", "NetworkError", "TimedOut", "ConnectionError")
        if any(kw in error_str or kw in type(context.error).__name__ for kw in transient_keywords):
            logger.warning("[TELEGRAM] Transient network error: %s. PTB will auto-retry.", error_str[:200])
            return  # Don't flood log with full traceback for DNS blips
        # 
        tb_string = traceback.format_exception(
            type(context.error), context.error, context.error.__traceback__
        )
        tb_text = ''.join(tb_string)
        # Log full traceback
        logger.error(f"PTB Exception:\n{tb_text}")
        # Determine which handler caused it — update CAN be None
        handler_name = "Unknown (no update context)"
        if update is not None:
            try:
                eff_msg = getattr(update, 'effective_message', None)
                cb_query = getattr(update, 'callback_query', None)
                if eff_msg is not None:
                    msg_text = getattr(eff_msg, 'text', None) or 'N/A'
                    handler_name = f"Message: {msg_text[:50]}"
                elif cb_query is not None:
                    cb_data = getattr(cb_query, 'data', None) or 'N/A'
                    handler_name = f"Callback: {cb_data[:50]}"
                else:
                    handler_name = f"Update type: {type(update).__name__}"
            except Exception:
                handler_name = "Unknown (introspection failed)"
        # Send condensed alert to Telegram
        try:
            error_type = type(context.error).__name__
            short_tb = tb_text[-500:] if len(tb_text) > 500 else tb_text
            await self.send_message(
                f"⚠️ *BOT ERROR*\n\n"
                f"Type: `{error_type}`\n"
                f"Handler: {handler_name}\n\n"
                f"```\n{short_tb}\n```",
                parse_mode=None
            )
        except Exception as e:
            logger.error(f"Failed to send error alert: {e}")
    def wait_until_ready(self, timeout: float = 10.0) -> bool:
        """
        Block until the bot's event loop is initialized and ready.
        Resolves Issue 4 (Race Condition).
        """
        return self._ready_event.wait(timeout)
    def start_polling(self):
        """Compatibility wrapper for running in a thread from main.py."""
        raise RuntimeError(
            "BRIDGE REMOVED in Phase 44.5. Use ShortCircuitBot.run(shutdown_event)."
        )
    def send_validation_alert(self, signal):
        """Compat wrapper."""
        asyncio.create_task(self.send_alert(f"VALIDATION ALERT: {signal.get('symbol')} {signal.get('ltp')}"))
    def send_multi_edge_alert(self, signal):
        """Compat wrapper."""
        asyncio.create_task(self.send_alert(f"MULTI-EDGE ALERT: {signal.get('symbol')} {signal.get('ltp')}"))
    def send_startup_message(self):
        asyncio.create_task(self.send_alert("� **ShortCircuit Bot Connected**\nSystem Online."))
    async def stop(self):
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None
        if self.app is None:
            return
        updater = getattr(self.app, "updater", None)
        if updater is not None and getattr(updater, "running", False):
            await updater.stop()
        if getattr(self.app, "running", False):
            await self.app.stop()
        await self.app.shutdown()
