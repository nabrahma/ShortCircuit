"""
The Telegram bot: transport, lifecycle, the auto-trade gate and the signal-card
flow. Commands live in `commands/`, message bodies in `cards.py`, and what the
bot knows about the active strategies in `book.py`.

The engine talks to the bot through a small, stable surface, used by the order
manager, focus engine, reconciliation, broker and supervisor:

    is_auto_mode()                     the auto-trade gate
    send_alert(text) / send_util_alert(text)
                                       queued Markdown alerts, throttled
    send_message(text, parse_mode)     one message, degrading to plain text
    queue_signal_discovery(signal)     a signal card, edited as the setup resolves
    queue_signal_validation_update(correlation_id, signal, outcome, details)
    send_validation_alert(signal)      a one-shot signal card
    attach_strategies(strategies)      the active plug-ins, for /strategies
    run(shutdown_event) / stop()

Auto mode:
- Boots from config.AUTO_MODE, which is intentionally True. The bot is armed on
  startup by design; do NOT "restore" a False default here. Telegram was
  unreliable on this connection (ISP blocking), so requiring a manual /auto on
  each morning meant missed sessions.
- Actual trading is still gated by config.TRADING_ENABLED, which MarketSession
  only sets True at 09:30 IST. Armed at boot != trading at boot.
- The ONLY thing that disarms it is an explicit /auto off. A max-session-loss
  halt blocks signals via SignalManager without touching this flag.
"""
from __future__ import annotations

import asyncio
import logging
import random
import threading
import time
import traceback
import uuid
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from telegram import BotCommand, BotCommandScopeDefault, Message, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from shortcircuit import config
from shortcircuit.eod import squareoff
from shortcircuit.observability.telegram import cards, fmt
from shortcircuit.observability.telegram import commands as command_registry
from shortcircuit.observability.telegram.book import StrategyBook
from shortcircuit.runtime import mode as run_mode

logger = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096
# A setup waits up to cards.PENDING_MINUTES for its trigger; keep its card's id
# a little longer so the outcome edits the card instead of posting a new one.
SIGNAL_CARD_TTL_SECS = cards.PENDING_MINUTES * 60 + 300

QUOTES = (
    "The goal of a successful trader is to make the best trades. Money is secondary. — Alexander Elder",
    "In trading, you have to be defensive and aggressive at the same time. — Paul Tudor Jones",
    "The trend is your friend until the end when it bends. — Ed Seykota",
    "The market is a device for transferring money from the impatient to the patient. — Warren Buffett",
    "Cut your losses. Let your profits run. — Jesse Livermore",
    "Focus on the process, not the outcome. — Mark Douglas",
    "Amateurs hope. Professionals have a plan. — Traditional",
    "Risk comes from not knowing what you're doing. — Warren Buffett",
    "You don't need to know what's going to happen next to make money. — Mark Douglas",
    "Trading is about odds, not certainties. — Traditional",
    "Patience is a weapon in the market. — Traditional",
    "It's not whether you're right or wrong, but how much you make when right and lose when wrong. — George Soros",
)


def _is_parse_failure(exc: Exception) -> bool:
    err = str(exc).lower()
    return ("can't parse" in err or "cant parse" in err or "entity" in err
            or "unsupported start tag" in err)


def _fit(text: str) -> str:
    return text if len(text) <= TELEGRAM_LIMIT else text[:TELEGRAM_LIMIT - 40] + "\n\n…(truncated)"


@dataclass
class SignalMsgState:
    created_at: float
    message_id: Optional[int] = None


class ShortCircuitBot:
    def __init__(self, config_settings: dict, order_manager, capital_manager, focus_engine=None):
        self.config = config_settings
        self.order_manager = order_manager
        self.capital_manager = capital_manager
        self.focus_engine = focus_engine
        # Injected by the supervisor after construction. Declared here so a
        # command arriving before wiring completes degrades instead of raising.
        self.signal_manager = None
        self.market_session = None

        self._auto_mode: bool = config_settings.get("AUTO_MODE", True)
        self._auto_on_queued: bool = False
        self._morning_brief_sent: bool = False

        self.bot_token = config_settings.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = str(config_settings.get("TELEGRAM_CHAT_ID"))
        self.app: Optional[Application] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready_event = threading.Event()
        self._shutdown_event: Optional[asyncio.Event] = None
        self._alert_queue: asyncio.Queue = asyncio.Queue()
        self._throttler_task: Optional[asyncio.Task] = None
        self._cleanup_task: Optional[asyncio.Task] = None

        self._editable_signal_flow_override: Optional[bool] = None
        # threading.Lock, not asyncio.Lock: a plain dict touched from both the
        # event loop and synchronous done-callbacks, never held across an await.
        self._signal_msg_index: dict[str, SignalMsgState] = {}
        self._signal_msg_index_lock = threading.Lock()
        self._scan_metadata: dict = {}

        self.book = StrategyBook()
        self.registry, self.command_errors = command_registry.build(
            tuple(config_settings.get("TELEGRAM_COMMAND_MODULES", ()) or ())
        )
        logger.info(
            "🤖 Telegram Bot initialized | Auto Mode: %s | %d commands%s",
            "ON" if self._auto_mode else "OFF", len(self.registry),
            f" | {len(self.command_errors)} command module(s) failed" if self.command_errors else "",
        )

    # The auto-trade gate

    def is_auto_mode(self) -> bool:
        """
        Single source of truth for the auto-trade gate.

        Also resolves a QUEUED activation: `/auto on` before 09:30 IST only sets
        `_auto_on_queued`, and resolving it here means it activates the moment
        any consumer asks (focus engine, order manager, /status) regardless of
        trading-loop timing.
        """
        if self._auto_on_queued and not self._auto_mode:
            now = fmt.now_ist()
            if (now.hour, now.minute) >= (9, 30):
                self._auto_mode = True
                self._auto_on_queued = False
                logger.critical("🟢 [AUTO] Queued Auto ON activated at %s IST", now.strftime("%H:%M:%S"))
                self._notify_threadsafe("✅ *Auto Mode activated* — 09:30 reached, scanning live")
        return self._auto_mode

    def arm_auto(self) -> Optional[int]:
        """Turn auto on now, or queue it for 09:30; minutes left when queued."""
        now = fmt.now_ist()
        earliest = now.replace(hour=9, minute=30, second=0, microsecond=0)
        if now < earliest:
            self._auto_on_queued = True
            return int((earliest - now).total_seconds() // 60)
        self._auto_mode = True
        self._auto_on_queued = False
        logger.critical("🟢 [AUTO] Auto-trading ENABLED via Telegram (/auto on)")
        return None

    def disarm_auto(self) -> None:
        self._auto_mode = False
        self._auto_on_queued = False
        logger.critical("🛑 [AUTO] Auto-trading DISABLED via Telegram override (/auto off)")

    def is_editable_signal_flow_enabled(self) -> bool:
        """One card per signal, edited as it resolves. Runtime override wins."""
        if self._editable_signal_flow_override is not None:
            return self._editable_signal_flow_override
        return bool(self.config.get("EDITABLE_SIGNAL_FLOW_ENABLED", True))

    # Strategies

    def attach_strategies(self, strategies: Iterable[Any]) -> None:
        self.book.attach(strategies)
        logger.info("[TELEGRAM] strategies attached: %s", ", ".join(self.book.names) or "none")

    # Text used by several commands

    def auto_text(self) -> str:
        if self.is_auto_mode():
            return "🟢 ON"
        return "⏳ OFF (queued for 09:30)" if self._auto_on_queued else "🔴 OFF (alerts only)"

    @staticmethod
    def run_text() -> str:
        return "🧪 DATA-ONLY (no orders)" if run_mode.is_data_only() else "💹 LIVE"

    def strategies_text(self) -> str:
        strategies = self.book.strategies
        if not strategies:
            return "none active"
        names = ", ".join(f"{fmt.he(s.name)} ({s.direction.value})" for s in strategies)
        paused = sum(self.book.is_paused(s.name) for s in strategies)
        return names + (f" · {paused} paused" if paused else "")

    def capital_block(self) -> str:
        if not self.capital_manager:
            return "Capital: N/A\n"
        try:
            real_margin = self.capital_manager._real_margin
            bp = self.capital_manager.buying_power
            slot = self.capital_manager.get_slot_status()
            slot_str = "🟢 FREE" if slot["slot_free"] else f"🔴 {fmt.he(slot['active_symbol'])}"
            return (f"Margin (live): <b>{fmt.money(real_margin) if real_margin > 0 else 'N/A'}</b>\n"
                    f"Buying power:  <b>{fmt.money(bp) if bp > 0 else 'N/A'}</b>\n"
                    f"Slot:          {slot_str}\n")
        except Exception as exc:
            logger.error("Failed to build capital block: %s", exc)
            return "Margin: data unavailable\n"

    def signal_block(self) -> str:
        if not self.signal_manager:
            return ""
        st = self.signal_manager.get_status()
        cb = "🔴 CIRCUIT BREAKER (MAX LOSS)" if st.get("is_paused") else "🟢 ACTIVE"
        t = self.book.totals()
        return (f"Signals:   {st.get('signals_sent', 0)} today | {cb}\n"
                f"Setups:    {t.line()}\n")

    def session_block(self) -> str:
        if not self.market_session:
            return ""
        try:
            state = self.market_session.get_current_state()
        except Exception:
            return ""
        label = {
            "PRE_MARKET": "🌅 PRE-MARKET", "EARLY_MARKET": "🌤️ EARLY (warmup)",
            "MID_MARKET": "☀️ PRIME", "EOD_WINDOW": "🌇 EOD WINDOW", "POST_MARKET": "🌙 CLOSED",
        }.get(state, state)
        return f"Session:    {fmt.he(label)}\n"

    def health_block(self) -> str:
        lines = []
        broker = getattr(self.order_manager, "broker", None) if self.order_manager else None
        if broker:
            data_ws = "✅" if getattr(broker, "data_ws_connected", False) else "❌"
            order_ws = "✅" if getattr(broker, "order_ws_connected", False) else "❌"
            lines.append(f"WS data {data_ws} · WS order {order_ws}")
        scan_time = self._scan_metadata.get("last_scan_time")
        if scan_time:
            names = self._scan_metadata.get("candidate_names", []) or []
            count = self._scan_metadata.get("candidate_count", 0)
            shown = ", ".join(fmt.he(n) for n in names[:10]) + ("…" if len(names) > 10 else "")
            lines.append(f"Last scan {scan_time.strftime('%H:%M:%S')}: {count} candidate(s)"
                         + (f" ({shown})" if count and names else ""))
        return "\n".join(lines) + "\n" if lines else ""

    def quote_snapshot(self) -> dict:
        broker = getattr(self.order_manager, "broker", None) if self.order_manager else None
        try:
            return dict(broker.get_quote_cache_snapshot()) if broker else {}
        except Exception:
            return {}

    def request_shutdown(self) -> None:
        if self._shutdown_event:
            self._shutdown_event.set()
        else:
            logger.error("[SHUTDOWN] No shutdown_event available; manual restart required")

    # Transport

    async def send_message(self, text: str, parse_mode="HTML", reply_markup=None) -> Optional[Message]:
        """
        Send to the authorized chat, degrading rather than dropping. Telegram
        rejects the whole message if the markup does not parse (an unescaped '<'
        in a broker error is enough), which would silently lose the alert saying
        a position is unprotected. So a parse failure is resent as plain text.
        """
        if not self.app:
            return None
        text = _fit(text)
        try:
            return await self.app.bot.send_message(chat_id=self.chat_id, text=text,
                                                   parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as exc:
            if parse_mode is not None and _is_parse_failure(exc):
                logger.warning("Telegram %s parse failed (%s); resending as plain text.",
                               parse_mode, str(exc)[:120])
                try:
                    return await self.app.bot.send_message(chat_id=self.chat_id, text=text,
                                                           parse_mode=None, reply_markup=reply_markup)
                except Exception as exc2:
                    logger.error("Telegram plain-text fallback also failed: %s", exc2)
                    return None
            logger.error("Telegram send_message failed: %s", exc)
            return None

    async def edit_message(self, message_id: int, text: str, parse_mode="HTML", reply_markup=None) -> bool:
        if not self.app:
            return False
        try:
            await self.app.bot.edit_message_text(chat_id=self.chat_id, message_id=message_id,
                                                 text=_fit(text), parse_mode=parse_mode,
                                                 reply_markup=reply_markup)
            return True
        except Exception as exc:
            if "Message is not modified" in str(exc):
                return True
            logger.error("Telegram edit_message failed: %s", exc)
            return False

    async def reply(self, update: Update, text: str, parse_mode: Optional[str] = "HTML", reply_markup=None):
        """Answer a command in its chat, degrading to plain text like send_message."""
        message = getattr(update, "effective_message", None) or getattr(update, "message", None)
        if message is None:
            return None
        text = _fit(text)
        try:
            return await message.reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
        except Exception as exc:
            if parse_mode is not None and _is_parse_failure(exc):
                logger.warning("Telegram reply parse failed (%s); resending as plain text.", str(exc)[:120])
                return await message.reply_text(text, parse_mode=None, reply_markup=reply_markup)
            raise

    def _notify_threadsafe(self, message: str) -> None:
        """Fire-and-forget alert from any thread (is_auto_mode runs on the focus thread too)."""
        loop = self._loop
        try:
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(self.send_alert(message), loop)
                return
            asyncio.get_running_loop().create_task(self.send_alert(message))
        except RuntimeError:
            logger.info("[TELEGRAM] Alert skipped (no loop): %s", message[:60])
        except Exception as exc:
            logger.warning("[TELEGRAM] threadsafe notify failed: %s", exc)

    async def send_alert(self, message: str):
        """Queue a Markdown alert for the throttler. Callers must await this."""
        if not self.app:
            return
        try:
            await self._alert_queue.put(message)
        except Exception as exc:
            logger.error("send_alert (queued) failed: %s", exc)

    async def send_util_alert(self, message: str):
        """Broker health alerts (FyersBrokerInterface). Same queue as send_alert."""
        await self.send_alert(message)

    async def _alert_throttler_loop(self):
        """Bundle bursts of alerts (up to 5 within 0.5 s) and rate-limit the sends."""
        logger.info("Telegram Alert Throttler started")
        last_send = 0.0
        while not self._shutdown_event or not self._shutdown_event.is_set():
            buffer: list[str] = []
            try:
                buffer.append(await self._alert_queue.get())
                try:
                    while len(buffer) < 5:
                        buffer.append(await asyncio.wait_for(self._alert_queue.get(), timeout=0.5))
                except asyncio.TimeoutError:
                    pass
                hz = getattr(config, "P81_TELEGRAM_RATE_LIMIT_HZ", 2) or 2
                wait = 1.0 / hz - (time.time() - last_send)
                if wait > 0:
                    await asyncio.sleep(wait)
                # Alerts across the engine are composed in Markdown (*BOLD*, `code`).
                sent = await self.send_message("\n\n".join(buffer), parse_mode="Markdown")
                if sent is None and len(buffer) > 1:
                    # One bad message would poison the bundle; resend the pieces.
                    logger.warning("[TELEGRAM] Bundle of %d failed; resending individually.", len(buffer))
                    for item in buffer:
                        await self.send_message(item, parse_mode="Markdown")
                last_send = time.time()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Throttler loop error: %s", exc)
                await asyncio.sleep(1)
            finally:
                # Always drain, even if the send raised, or the same alerts repeat.
                for _ in range(len(buffer)):
                    try:
                        self._alert_queue.task_done()
                    except ValueError:
                        break

    # Signal cards

    def _margin_text(self, signal: dict) -> Optional[str]:
        """Estimated margin: the signal's own qty if sized, else the capital manager's sizing."""
        try:
            qty = int(signal.get("quantity") or 0)
            entry = float(signal.get("entry_price") or 0)
            if qty > 0 and entry > 0:
                lev = float(getattr(self.capital_manager, "leverage", None)
                            or getattr(config, "INTRADAY_LEVERAGE", 5.0) or 5.0)
                return f"{fmt.money(qty * entry / lev)} (qty {qty})"
            ltp = float(signal.get("ltp") or 0)
            if self.capital_manager and ltp > 0:
                qty, _, margin = self.capital_manager.compute_qty(signal.get("symbol"), ltp)
                return f"{fmt.money(margin)} (qty {qty})"
        except Exception as exc:
            logger.debug("margin estimate failed: %s", exc)
        return None

    def _armed_text(self, signal: dict) -> str:
        return cards.signal_armed(signal, auto=self.is_auto_mode(), data_only=run_mode.is_data_only(),
                                  margin=self._margin_text(signal))

    def _outcome_text(self, signal: dict, outcome: str, details: Optional[dict]) -> str:
        return cards.signal_outcome(signal, outcome, details, auto=self.is_auto_mode(),
                                    data_only=run_mode.is_data_only(), margin=self._margin_text(signal))

    async def send_signal_discovery(self, signal: dict) -> Optional[int]:
        msg = await self.send_message(self._armed_text(signal))
        return msg.message_id if msg else None

    async def queue_signal_discovery(self, signal: dict) -> str:
        """Post the signal's card without blocking the scan; returns its correlation id."""
        self.book.record(signal, "armed")
        correlation_id = str(uuid.uuid4())
        with self._signal_msg_index_lock:
            self._signal_msg_index[correlation_id] = SignalMsgState(created_at=time.time())
        if not self.app:
            logger.warning("[EDITABLE] Bot not ready for discovery queue")
            return correlation_id
        task = asyncio.create_task(self.send_signal_discovery(signal))

        def _on_done(done):
            try:
                message_id = done.result()
            except asyncio.CancelledError:
                return
            except Exception as exc:
                logger.error("[EDITABLE] Discovery send failed: %s", exc)
                return
            if message_id is None:
                return
            with self._signal_msg_index_lock:
                state = self._signal_msg_index.get(correlation_id)
                if state is not None:
                    state.message_id = message_id

        task.add_done_callback(_on_done)
        return correlation_id

    async def queue_signal_validation_update(self, correlation_id: str, signal: dict, outcome: str,
                                             details: Optional[dict] = None) -> None:
        """Edit the signal's card with its outcome, without blocking the caller."""
        self.book.record_outcome(signal, outcome)
        if not self.app:
            logger.warning("[EDITABLE] Bot not ready for validation update (%s)", correlation_id)
            return
        asyncio.create_task(self._handle_signal_validation_update(correlation_id, signal, outcome, details))

    async def _handle_signal_validation_update(self, correlation_id: str, signal: dict, outcome: str,
                                               details: Optional[dict] = None) -> None:
        with self._signal_msg_index_lock:
            state = self._signal_msg_index.get(correlation_id)
            message_id = state.message_id if state else None
        text = self._outcome_text(signal, outcome, details)
        try:
            edited = await self.edit_message(message_id, text) if message_id else False
            if not edited:
                await self.send_message(text)
        except Exception as exc:
            logger.error("[EDITABLE] Validation update failed (%s): %s", correlation_id, exc)
        finally:
            with self._signal_msg_index_lock:
                self._signal_msg_index.pop(correlation_id, None)

    def send_validation_alert(self, signal: dict) -> None:
        """A one-shot signal card, for when the edited-card flow is off."""
        self.book.record(signal, "armed")
        try:
            asyncio.get_running_loop().create_task(self.send_message(self._armed_text(signal)))
        except RuntimeError:
            logger.info("[TELEGRAM] signal card skipped (no loop): %s", signal.get("symbol"))

    async def _cleanup_stale_signal_entries(self, now: Optional[float] = None) -> int:
        now_ts = now if now is not None else time.time()
        with self._signal_msg_index_lock:
            stale = [k for k, s in self._signal_msg_index.items() if now_ts - s.created_at > SIGNAL_CARD_TTL_SECS]
            for k in stale:
                self._signal_msg_index.pop(k, None)
        return len(stale)

    async def _cleanup_stale_signal_entries_loop(self):
        while True:
            try:
                removed = await self._cleanup_stale_signal_entries()
                if removed:
                    logger.info("[EDITABLE] Cleaned %d stale signal message entries", removed)
                await asyncio.sleep(600)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[EDITABLE] Stale cleanup loop failed: %s", exc)
                await asyncio.sleep(600)

    # Morning briefing

    async def _send_morning_briefing(self, ws_cache, market_ctx, startup_validation_passed: bool):
        """Once per session, when the trading loop starts."""
        if self._morning_brief_sent:
            return
        self._morning_brief_sent = True
        now = fmt.now_ist()
        if market_ctx and getattr(market_ctx, "morning_range_valid", False):
            hi, lo = getattr(market_ctx, "morning_high", 0.0), getattr(market_ctx, "morning_low", 0.0)
            range_lines = [f"High {hi:,.2f} · Low {lo:,.2f} · span {hi - lo:,.2f} pts"]
        else:
            range_lines = ["⚠️ unavailable (fetched after the open)"]
        snap = ws_cache.cache_health_snapshot() if ws_cache and hasattr(ws_cache, "cache_health_snapshot") else {}
        fresh, total = snap.get("fresh", 0), snap.get("total", 0)
        cache_text = f"WS cache {fresh}/{total} live ({fresh / total * 100:.1f}%)" if total else "WS cache ⚠️ not primed"
        if squareoff.registry.loaded:
            sq = f"F&O stocks {squareoff.cas_squareoff().strftime('%H:%M')}, others " \
                 f"{squareoff.ENGINE_SQUAREOFF.strftime('%H:%M')}"
        else:
            sq = f"F&O list unavailable: every position {squareoff.ENGINE_SQUAREOFF.strftime('%H:%M')}"
        text = cards.morning_brief(
            date_text=now.strftime("%A, %d %b %Y"), time_text=now.strftime("%H:%M IST"),
            quote=random.choice(QUOTES), range_lines=range_lines,
            # Read the actual socket state: this once printed both as healthy
            # unconditionally, on exactly the mornings they were down.
            data_ok=bool(getattr(ws_cache, "data_ws_connected", False)),
            order_ok=bool(getattr(ws_cache, "order_ws_connected", False)),
            cache_text=cache_text, candle_ok=startup_validation_passed,
            auto_text=self.auto_text(), run_text=self.run_text(),
            strategy_names=[f"{s.name} ({s.direction.value})" for s in self.book.strategies],
            squareoff_text=sq,
        )
        await self.send_message(text)
        logger.info("[TELEGRAM] Morning briefing sent")

    # Authorization

    def _is_authorized(self, update: Update) -> bool:
        """Only the configured TELEGRAM_CHAT_ID may command the bot, even if the token leaks."""
        chat = getattr(update, "effective_chat", None)
        if not chat:
            return False
        if str(chat.id) != self.chat_id:
            logger.warning("⚠️ Unauthorized command attempt from chat_id: %s", chat.id)
            return False
        return True

    def _is_authorized_query(self, query) -> bool:
        """Inline-button presses: the user id (private chat) or the chat id (group)."""
        if str(query.from_user.id) == self.chat_id:
            return True
        chat_id = str(query.message.chat.id) if query.message else None
        if chat_id != self.chat_id:
            logger.warning("⚠️ Unauthorized button press from user_id: %s", query.from_user.id)
            return False
        return True

    # Handlers

    def _guarded(self, command: command_registry.Command):
        """Every command, built-in or plugged in, passes the same gate."""
        async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            if not self._is_authorized(update):
                return
            try:
                await command.handler(self, update, context)
            except Exception as exc:
                logger.exception("[TELEGRAM] /%s failed", command.name)
                try:
                    await self.reply(update, f"❌ /{command.name} failed: "
                                             f"{fmt.he(type(exc).__name__)}: {fmt.he(exc)}")
                except Exception:
                    pass
        return handler

    async def _handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if not self._is_authorized_query(query):
            return
        parts = (query.data or "").split(":")
        handler = self.registry.callback(parts[0])
        if handler is None:
            logger.warning("[TELEGRAM] no handler for button %r", query.data)
            return
        await handler(self, query, parts)

    def _register_handlers(self):
        for command in self.registry:
            self.app.add_handler(CommandHandler(command.name, self._guarded(command)))
        self.app.add_handler(CallbackQueryHandler(self._handle_callback))

    async def _error_handler(self, update: object, context: ContextTypes.DEFAULT_TYPE):
        """Global PTB error handler. `update` is None for polling and network errors."""
        error_str = str(context.error)
        transient = ("getaddrinfo", "NetworkError", "TimedOut", "ConnectionError")
        if any(k in error_str or k in type(context.error).__name__ for k in transient):
            logger.warning("[TELEGRAM] Transient network error: %s. PTB will auto-retry.", error_str[:200])
            return
        tb = "".join(traceback.format_exception(type(context.error), context.error, context.error.__traceback__))
        logger.error("PTB Exception:\n%s", tb)
        where = "Unknown (no update context)"
        if update is not None:
            try:
                msg = getattr(update, "effective_message", None)
                cbq = getattr(update, "callback_query", None)
                if msg is not None:
                    where = f"Message: {(getattr(msg, 'text', None) or 'N/A')[:50]}"
                elif cbq is not None:
                    where = f"Callback: {(getattr(cbq, 'data', None) or 'N/A')[:50]}"
                else:
                    where = f"Update type: {type(update).__name__}"
            except Exception:
                where = "Unknown (introspection failed)"
        try:
            await self.send_message(
                f"⚠️ BOT ERROR\n\nType: {type(context.error).__name__}\nHandler: {where}\n\n{tb[-500:]}",
                parse_mode=None,
            )
        except Exception as exc:
            logger.error("Failed to send error alert: %s", exc)

    # Lifecycle

    async def start(self):
        self.app = Application.builder().token(self.bot_token).build()
        self._register_handlers()
        self.app.add_error_handler(self._error_handler)
        await self.app.initialize()
        await self.app.start()
        self._loop = asyncio.get_running_loop()
        self._ready_event.set()
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
        self._cleanup_task = asyncio.create_task(self._cleanup_stale_signal_entries_loop())
        await self.app.updater.start_polling(drop_pending_updates=True)

        if getattr(config, "P81_TELEGRAM_MENU_ENABLED", True):
            try:
                await self.app.bot.set_my_commands(
                    [BotCommand(name, summary) for name, summary in self.registry.menu()],
                    scope=BotCommandScopeDefault(),
                )
                logger.info("Telegram Command Menu registered")
            except Exception as exc:
                logger.error("Failed to register Telegram Menu: %s", exc)

        self._throttler_task = asyncio.create_task(self._alert_throttler_loop())
        if self.command_errors:
            await self.send_message("⚠️ <b>Command modules not loaded</b>\n" + "\n".join(
                f"<code>{fmt.he(m)}</code>: {fmt.he(e)}" for m, e in self.command_errors.items()))
        logger.info("Telegram Bot started & Throttler active")

    async def run(self, shutdown_event: asyncio.Event):
        """Start polling, wait for shutdown_event, then guarantee PTB teardown."""
        self._shutdown_event = shutdown_event
        await self.start()
        try:
            await shutdown_event.wait()
        finally:
            logger.info("[TELEGRAM] Shutdown event received, stopping PTB.")
            await self.stop()

    async def stop(self):
        for task in (self._cleanup_task, self._throttler_task):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self._cleanup_task = self._throttler_task = None
        if self.app is None:
            return
        updater = getattr(self.app, "updater", None)
        if updater is not None and getattr(updater, "running", False):
            await updater.stop()
        if getattr(self.app, "running", False):
            await self.app.stop()
        await self.app.shutdown()
