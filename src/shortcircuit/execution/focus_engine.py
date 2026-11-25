import time
import logging
import threading
import datetime
import asyncio
import pytz
from dataclasses import dataclass
from typing import Optional

from shortcircuit.broker.fyers_connect import FyersConnect
from shortcircuit import config
from shortcircuit.eod import squareoff
from shortcircuit.runtime import mode as run_mode
from shortcircuit.strategy import features as F
from shortcircuit.strategy.sdk import RsiCover, StagedRsiExit
from shortcircuit.execution.order_manager import OrderManager

from shortcircuit.observability.gate_result_logger import get_gate_result_logger

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("FocusEngine")

# Minimum spacing between hard-stop orderbook checks from the focus loop.
# See FocusEngine._hard_stop_poll_due for why this is not every tick.
HARD_STOP_POLL_INTERVAL_S = 2.0


def rsi_cover_of(trade: dict) -> Optional[RsiCover]:
    """The RSI relief-rally cover a position's strategy asked for, if any."""
    params = (trade.get('exit_params') or {}).get('rsi_cover')
    if not params:
        return None
    try:
        return RsiCover(**params)
    except TypeError:
        logger.error("[RSI-TP] malformed rsi_cover %r — rule disabled for this trade", params)
        return None


def staged_rsi_of(trade: dict) -> Optional[StagedRsiExit]:
    """The staged RSI exits a position's strategy asked for, if any."""
    params = (trade.get('exit_params') or {}).get('staged_rsi')
    if not params:
        return None
    try:
        return StagedRsiExit(**params)
    except (TypeError, ValueError):
        logger.error("[STAGED] malformed staged_rsi %r — rules disabled for this trade", params)
        return None


@dataclass(frozen=True)
class Bar:
    epoch: int          # bar open, epoch seconds
    high: float
    close: float


SESSION_OPEN_MINUTE = 9 * 60 + 15


def completed_bars(candles, minutes: int, now_epoch: float) -> list:
    """
    Aggregate 1-minute candles into `minutes`-wide bars aligned to the 09:15
    open, keeping only bars that have fully closed by `now_epoch`. Bars missing
    minutes are kept — a quiet minute with no trade is still part of the bar.
    """
    buckets: dict = {}
    order: list = []
    for c in candles:
        epoch = int(c.epoch)
        ist_minute = ((epoch + 19800) // 60) % 1440          # +05:30
        offset = (ist_minute - SESSION_OPEN_MINUTE) // minutes * minutes
        start = epoch - (ist_minute - SESSION_OPEN_MINUTE - offset) * 60
        b = buckets.get(start)
        if b is None:
            buckets[start] = [float(c.high), float(c.close)]
            order.append(start)
        else:
            b[0] = max(b[0], float(c.high))
            b[1] = float(c.close)
    return [Bar(s, buckets[s][0], buckets[s][1]) for s in order
            if s + minutes * 60 <= now_epoch]


def target_reached(ltp: float, level, direction: str = 'SHORT') -> bool:
    """
    Has price reached a take-profit level?

    A SHORT target sits below entry and is reached on the way down; a LONG
    target sits above and is reached on the way up. `level` is None whenever
    TP_MODE is 'OFF' or the computed target landed on the wrong side of entry,
    and None is never "reached" — that is what lets a trade run on its stop
    alone instead of closing the instant it opens.
    """
    if level is None or not ltp:
        return False
    return (ltp >= level) if direction == 'LONG' else (ltp <= level)


def compute_tp_levels(entry_price: float, vwap_target, tick: float, is_long: bool):
    """
    Turn the VWAP mean-reversion target into the two levels the engine trades.

    Returns (tp_1, tp_2): the midpoint between entry and the target, and the
    target itself, both snapped to the instrument's tick.

    Returns (None, None) when the target is not strictly beyond entry — which
    happens when VWAP has already been crossed by the time the fill lands. A
    target on the wrong side of entry would be "reached" on the first tick and
    close the position immediately; None means the trade runs on its stop.
    """
    if not entry_price or entry_price <= 0 or vwap_target is None:
        return None, None
    if (is_long and vwap_target <= entry_price) or (not is_long and vwap_target >= entry_price):
        return None, None
    t = tick if tick and tick > 0 else 0.05
    tp_2 = round(round(vwap_target / t) * t, 2)
    tp_1 = round(round((entry_price + (vwap_target - entry_price) / 2.0) / t) * t, 2)
    return tp_1, tp_2


class FocusEngine:
    def __init__(self, trade_manager=None, order_manager=None, discretionary_engine=None):
        self.fyers = FyersConnect().authenticate()
        self.trade_manager = trade_manager 
        
        self.order_manager = order_manager
        self.discretionary_engine = discretionary_engine
        
        self.active_trade = None # Reference to OrderManager position
        # Backoff state for exits that declined to complete — see _retire_or_retry.
        self._exit_retry_at: dict[str, float] = {}
        self._exit_retry_count: dict[str, int] = {}
        self.is_running = False
        self.telegram_bot = None # Injected by main.py
        
        # Validation gate and cooldown queue
        self.pending_signals = {} # {symbol: {signal_data, entry_trigger, invalidation_trigger, timestamp}}
        self.cooldown_signals = {} # {symbol: {data, unlock_at}}
        self.monitoring_active = False
        self.monitor_thread = None
        
        self.attempt_recovery()
        
        # Event loop reference for sync thread async dispatch
        self._event_loop = None
        

    def add_pending_signal(self, signal_data):
        """
        Adds a signal to the Validation Gate.
        It will ONLY be executed if Price breaks Signal Low (Short).
        """
        symbol = signal_data['symbol']
        signal_low = signal_data.get('signal_low')
        if not signal_low:
            logger.error(f"Cannot validate {symbol}: Missing signal_low")
            return

        # A short arms when price breaks the signal candle's low, and is
        # invalidated by a 0.2% push above its high.
        entry_trigger = signal_low
        signal_high = signal_data.get('signal_high', signal_low * 1.01)
        invalidation_trigger = signal_high * 1.002

        # Fixed 15-minute expiry.
        IST = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.datetime.now(IST)
        expires_at = now_ist + datetime.timedelta(minutes=15)

        self.pending_signals[symbol] = {
            'data': signal_data,
            'trigger': entry_trigger,
            'invalidate': invalidation_trigger,
            'timestamp': time.time(),
            'expires_at': expires_at, # dynamic timeout
            'queued_at': datetime.datetime.now(),  # for stale signal flush at 9:45
            'correlation_id': signal_data.get('correlation_id'),
            'last_evaluated_minute': None, # for candle-close validation
        }
        
        # Trigger immediate cooldown for this symbol in SignalManager
        # This prevents other scanner instances (if parallel) from picking it up
        if hasattr(self, 'analyzer') and self.analyzer:
            try:
                self.analyzer.signal_manager.add_pending_signal(symbol)
            except Exception as e:
                logger.error(f"[GATE] Failed to set G8.3 cooldown for {symbol}: {e}")

        logger.info(f"[GATE] Added {symbol} to Validation Gate. Trigger: < {entry_trigger}")
        
        if not self.monitoring_active:
            self.start_pending_monitor()

    def flush_stale_pending_signals(self, max_age_minutes: int = 20):
        """
        FIX #5: Called at 9:45 session boundary.
        Drops any pending signal older than max_age_minutes to prevent stale-price execution.
        """
        now = datetime.datetime.now()
        stale_keys = []
        for symbol, pending in self.pending_signals.items():
            queued_at = pending.get('queued_at')
            if queued_at:
                age_min = (now - queued_at).total_seconds() / 60
                if age_min > max_age_minutes:
                    stale_keys.append(symbol)
                    logger.info(f"[GATE] FLUSHED stale pending signal {symbol} — age {age_min:.1f}min")
        for k in stale_keys:
            self.pending_signals.pop(k, None)

    def stop(self, reason: str = "SHUTDOWN"):
        """
        Hard stop for the validation monitor. Called at EOD or on shutdown.
        Clears all queues, cancels the async monitor task, stops the sync thread.
        """
        logger.info(f"[GATE] FocusEngine.stop() called — reason: {reason}")
        self.monitoring_active = False
        self.pending_signals.clear()
        self.cooldown_signals.clear()

        # Cancel async task if running
        task = getattr(self, '_monitor_task', None)
        if task and not task.done():
            task.cancel()
            logger.info("[GATE] Monitor task cancelled.")

        # Stop sync fallback thread (it checks monitoring_active)
        thread = getattr(self, 'monitor_thread', None)
        if thread and thread.is_alive():
            logger.info("[GATE] Sync monitor thread will exit on next iteration.")

    def queue_cooldown_signal(self, signal_data, unlock_at):
        """Phase 43.4: Queues a signal that passed gates but hit cooldown."""
        symbol = signal_data['symbol']
        self.cooldown_signals[symbol] = {
            'data': signal_data,
            'unlock_at': unlock_at
        }
        if not self.monitoring_active:
            self.start_pending_monitor()

    def flush_pending_signals(self):
        """Phase 43.4: Promotes signals whose cooldown has expired."""
        now = datetime.datetime.now()
        
        if now.hour == 15 and now.minute >= 10:
            if self.cooldown_signals:
                logger.info("EOD Window active - clearing pending cooldown signals.")
                self.cooldown_signals.clear()
            return
            
        for symbol, meta in list(self.cooldown_signals.items()):
            if now >= meta['unlock_at']:
                # Re-validate live gain before promoting
                ltp = 0
                open_val = 0
                if self.order_manager and self.order_manager.broker:
                    snapshot = self.order_manager.broker.get_quote_cache_snapshot()
                    if symbol in snapshot:
                        entry = snapshot[symbol]
                        ltp = entry['ltp']
                        open_val = entry['open']
                
                if ltp == 0:
                    try:
                        data = {"symbols": symbol}
                        resp = self.fyers.quotes(data=data)
                        if 'd' in resp and resp['d']:
                            ltp = resp['d'][0]['v']['lp']
                            open_val = resp['d'][0]['v']['open_price']
                    except Exception as e:
                        logger.error(f"Failed to re-evaluate cooldown signal {symbol}: {e}")
                        continue

                if open_val > 0:
                    gain = ((ltp - open_val) / open_val) * 100
                    # The threshold is a positive scalar, so compare on magnitude.
                    if abs(gain) >= config.DAY_GAIN_PCT_THRESHOLD: 
                        logger.info(f"PROMOTED {symbol} from pending — cooldown expired, gain {gain:.2f}%")
                        self.add_pending_signal(meta['data'])
                    else:
                        logger.info(f"DROPPED {symbol} from pending — gain {gain:.2f}% < threshold")
                        
                del self.cooldown_signals[symbol]

    def start_pending_monitor(self):
        """Starts the async background task for validation checks."""
        if self.monitoring_active:
            return
        self.monitoring_active = True
        # explicitly pass loop to fallback thread to fix Python 3.12 RuntimeError
        try:
            loop = asyncio.get_running_loop()
            if loop.is_running():
                self._monitor_task = loop.create_task(self.monitor_pending_loop())
            else:
                self._monitor_task = loop.create_task(self.monitor_pending_loop())
        except RuntimeError:
            logger.warning("[GATE] No asyncio loop — using threaded monitor fallback")
            self._monitor_task = None
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
            self.monitor_thread = threading.Thread(
                target=self._monitor_pending_loop_sync, 
                args=(loop,),
                daemon=True
            )
            self.monitor_thread.start()
        logger.info("[GATE] Validation Monitor Started.")

    def _monitor_pending_loop_sync(self, loop: asyncio.AbstractEventLoop):
        """Fallback sync monitor that dispatches to async via run_coroutine_threadsafe."""
        while self.monitoring_active:
            try:
                if self.pending_signals:
                    future = asyncio.run_coroutine_threadsafe(
                        self.check_pending_signals(self.trade_manager), loop
                    )
                    future.result(timeout=30)
                if self.cooldown_signals:
                    self.flush_pending_signals()
            except Exception as e:
                logger.error(f"Sync Monitor Loop Error: {e}")
            time.sleep(2)

    async def monitor_pending_loop(self):
        """Async background loop. Stops automatically at EOD."""
        while self.monitoring_active:

            # EOD guard: kill the loop at 15:10.
            now = datetime.datetime.now()
            if now.hour == 15 and now.minute >= 10:
                logger.info("[GATE] EOD: 15:10 reached — stopping validation monitor.")
                self.stop("EOD_TIME_BOUNDARY")
                return

            if self.cooldown_signals:
                try:
                    await asyncio.to_thread(self.flush_pending_signals)
                except Exception as e:
                    logger.error(f"Cooldown flush error: {e}")

            if not self.pending_signals:
                await asyncio.sleep(5)
                continue

            try:
                await self.check_pending_signals(self.trade_manager)
            except Exception as e:
                logger.error(f"Monitor Loop Error: {e}")

            await asyncio.sleep(0.5)

    async def check_pending_signals(self, trade_manager):
        """
        Monitors pending signals for Validation Trigger.
        FIX #1: Now async. FIX #3: Slot guard + burn-after-confirm.
        """
        if not self.pending_signals:
            return

        # EOD guard: never execute after 15:10.
        now = datetime.datetime.now()
        if now.hour == 15 and now.minute >= 10:
            logger.info(f"[GATE] EOD guard triggered in check_pending_signals. "
                        f"Clearing {len(self.pending_signals)} pending signals.")
            self.stop("EOD_CHECK_GUARD")
            return

        current_pending = list(self.pending_signals.items())
        
        for symbol, pending in current_pending:
            try:
                trigger_price = pending['trigger']
                inval_price = pending['invalidate']
                
                # G12 candle-close validation
                use_close = getattr(config, 'P58_G12_USE_CANDLE_CLOSE', False)
                IST = pytz.timezone('Asia/Kolkata')
                now_ist = datetime.datetime.now(IST)
                
                if use_close:
                    current_minute = now_ist.replace(second=0, microsecond=0)
                    last_eval = pending.get('last_evaluated_minute')
                    
                    if last_eval is not None and current_minute <= last_eval:
                        continue # Already evaluated this minute boundary
                    
                    # New minute boundary reached - attempt to fetch last closed candle
                    today = now_ist.strftime("%Y-%m-%d")
                    hist_data = {
                        "symbol": symbol,
                        "resolution": "1",
                        "date_format": "1",
                        "range_from": today,
                        "range_to": today,
                        "cont_flag": "1"
                    }
                    hist_resp = await asyncio.to_thread(self.fyers.history, data=hist_data)
                    if hist_resp.get('s') == 'ok' and hist_resp.get('candles'):
                        last_candle = hist_resp['candles'][-1]
                        
                        # Verify this is actually the candle that just closed
                        # (timestamp should be current_minute - 1 min)
                        expected_ts = int(current_minute.timestamp()) - 60
                        if last_candle[0] < expected_ts:
                            continue # Wait for Fyers to post the candle
                        
                        ltp = last_candle[4]
                        pending['last_evaluated_minute'] = current_minute
                        
                        logger.info(f"[GATE] {symbol} Minute-End Close: ₹{ltp} (Trigger: ₹{trigger_price}, Inval: ₹{inval_price})")
                    else:
                        continue 
                else:
                    # WebSocket Cache-First LTP-touch logic
                    ltp = 0
                    if self.order_manager and self.order_manager.broker:
                        snapshot = self.order_manager.broker.get_quote_cache_snapshot()
                        if symbol in snapshot:
                            ltp = snapshot[symbol]['ltp']
                    
                    if ltp == 0:
                        # Fallback to direct REST if cache miss
                        data = {"symbols": symbol}
                        resp = await asyncio.to_thread(self.fyers.quotes, data=data)
                        if 'd' not in resp or not resp['d']: continue
                        ltp = resp['d'][0]['v']['lp']
                timestamp = pending['timestamp']

                def _queue_validation_update(outcome, details=None):
                    correlation_id = pending.get('correlation_id')
                    if not correlation_id:
                        return
                    if self.telegram_bot and hasattr(self.telegram_bot, 'queue_signal_validation_update'):
                        # None message_id is expected on very fast validations; bot falls back
                        # to a fresh message when discovery send has not completed yet.
                        asyncio.create_task(self.telegram_bot.queue_signal_validation_update(
                            correlation_id=correlation_id,
                            signal=pending['data'],
                            outcome=outcome,
                            details=details or {}
                        ))
                
                # Trigger check: for a short, LTP below the signal low.
                if ltp < trigger_price:
                    # G10.1: Execution Precision (Spread Guard)
                    # Soft Gate: Downgrades to CAUTIOUS mode if spread is wide.
                    spread_pct = 0.0
                    try:
                        depth_resp = await asyncio.to_thread(self.fyers.depth, data={"symbol": symbol})
                        if 'd' in depth_resp and symbol in depth_resp['d']:
                            depth = depth_resp['d'][symbol]
                            ask = depth['ask'][0]['price'] if depth['ask'] else ltp
                            bid = depth['bid'][0]['price'] if depth['bid'] else ltp
                            spread_pct = (ask - bid) / ltp if ltp > 0 else 0
                            
                            if spread_pct > 0.004:
                                logger.warning(f"⚠️ [WIDE SPREAD] {symbol} spread {spread_pct:.4f} > 0.004 | Downgraded to CAUTIOUS")
                                pending['data']['execution_mode'] = 'CAUTIOUS'
                            else:
                                pending['data']['execution_mode'] = pending['data'].get('execution_mode', 'NORMAL')
                    except Exception as e:
                        logger.warning(f"G10 Spread check failed (non-fatal) for {symbol}: {e}")

                    # G10.2: Entry Price = signal_low - 1 tick
                    tick_size = pending['data'].get('tick_size', 0.05)
                    adjusted_entry = trigger_price - tick_size
                    pending['data']['adjusted_entry'] = adjusted_entry
                    # Carried to the fill log: slippage is only meaningful against
                    # the level that actually triggered the order.
                    pending['data']['gate_trigger'] = trigger_price

                    # Record the gate outcome.
                    _gr = pending.get('data', {}).get('_gate_result')
                    if _gr is not None:
                        _gr.g10_pass  = True
                        _gr.g11_pass  = True   # Will re-evaluate below
                        _gr.g12_pass  = True
                        _gr.g12_value = round(trigger_price - ltp, 4)

                    _queue_validation_update(
                        outcome='VALIDATED',
                        details={
                            'reason': 'GATE12_TRIGGER_BROKEN',
                            'trigger_price': trigger_price,
                            'ltp': ltp,
                            'entry_price': pending['data'].get('adjusted_entry')
                        }
                    )

                    # Auto-mode gate
                    auto_enabled = False
                    if hasattr(self, 'telegram_bot') and self.telegram_bot:
                        auto_enabled = self.telegram_bot.is_auto_mode()

                    if not auto_enabled:
                        if _gr is not None:
                            _gr.verdict = "SUPPRESSED"
                            _gr.rejection_reason = "Auto mode OFF — signal alerted manually"
                            get_gate_result_logger().record(_gr)
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
                            asyncio.create_task(self.telegram_bot.send_alert(msg))
                        del self.pending_signals[symbol]
                        continue

                    logger.info(f"✅ [VALIDATED] {symbol} broke {trigger_price} @ {ltp}. Checking capital...")

                    # Capital slot check
                    # A signal is fully observed even when capital is locked: it still
                    # sends its Telegram alert (with a NOT TAKEN footer) and still logs
                    # as OBSERVED_NO_CAPITAL, so the ML set is not biased toward the
                    # trades that happened to find a free slot.
                    capital = getattr(self.order_manager, 'capital', None) if self.order_manager else None

                    if capital and not capital.is_slot_free:
                        active_sym = capital.active_symbol or "unknown"
                        cap_status = capital.get_slot_status()

                        logger.info(
                            f"📊 [OBSERVED — NOT TAKEN] {symbol} | "
                            f"trigger broken @ ₹{ltp} (trigger=₹{trigger_price}) | "
                            f"capital slot occupied by {active_sym}"
                        )

                        # Send same-format signal alert but with NOT TAKEN footer
                        if self.telegram_bot:
                            sig_data = pending.get('data', {})
                            asyncio.create_task(self.telegram_bot.send_alert(
                                f"📊 **SIGNAL PASSED — NOT TAKEN**\n\n"
                                f"Symbol:   `{symbol}`\n"
                                f"Trigger:  ₹{trigger_price} → LTP ₹{ltp}\n"
                                f"Signal:   {sig_data.get('signal_type', 'SHORT')}\n"
                                f"Gain:     {sig_data.get('day_gain_pct', 0):.2f}%\n"
                                f"RVOL:     {sig_data.get('rvol', 0):.1f}x\n\n"
                                f"❌ *Not Executed — Capital Locked*\n"
                                f"Active:   `{active_sym}`\n"
                                f"Margin:   ₹{cap_status['real_margin']:.2f}\n"
                                f"Last Sync: {cap_status['last_sync']}\n\n"
                                f"_Will trade again after `{active_sym}` position closes._"
                            ))

                        # Log as OBSERVED_NO_CAPITAL for ML data (not REJECTED — it genuinely passed)
                        if _gr is not None:
                            _gr.g11_pass = False
                            _gr.verdict = "OBSERVED_NO_CAPITAL"
                            _gr.rejection_reason = f"Capital slot occupied by {active_sym} — signal valid but not executed"
                            get_gate_result_logger().record(_gr)

                        del self.pending_signals[symbol]
                        continue  # Move to next pending symbol

                    # Execution cooldown check
                    if self.order_manager:
                        cooldown_active, remaining_secs = self.order_manager.is_exec_cooldown_active(symbol)
                        if cooldown_active:
                            logger.info(
                                f"⏳ [EXEC COOLDOWN] {symbol} | {remaining_secs}s remaining | "
                                f"skipping execution but monitoring continues"
                            )
                            # Don't delete from pending — keep monitoring
                            # (cooldown protects execution but not observation)
                            if self.telegram_bot:
                                asyncio.create_task(self.telegram_bot.send_alert(
                                    f"⏳ **SIGNAL PASSED — COOLDOWN ACTIVE**\n\n"
                                    f"Symbol: `{symbol}`\n"
                                    f"Trigger: ₹{trigger_price} → LTP ₹{ltp}\n"
                                    f"⏳ Execution blocked: {remaining_secs // 60}m {remaining_secs % 60}s remaining\n"
                                    f"_(Previous entry attempt failed)_"
                                ))
                            del self.pending_signals[symbol]
                            continue

                    # Slot guard (SignalManager)
                    analyzer = getattr(self, 'analyzer', None)
                    if analyzer and hasattr(analyzer, 'signal_manager'):
                        can_trade, reason = analyzer.signal_manager.can_signal(symbol, is_execution=True)
                        if not can_trade:
                            logger.info(f"🚫 [SLOT BLOCKED] {symbol} — {reason}")
                            if _gr is not None:
                                _gr.verdict = "REJECTED"
                                _gr.rejection_reason = f"Slot guard: {reason}"
                                get_gate_result_logger().record(_gr)
                            del self.pending_signals[symbol]
                            continue

                    # OrderManager guard
                    if self.order_manager is None:
                        if _gr is not None:
                            _gr.verdict = "DATA_ERROR"
                            _gr.rejection_reason = "OrderManager not initialized at execution time"
                            get_gate_result_logger().record(_gr)
                        logger.critical(
                            "[FATAL] OrderManager is None at execution time for %s. "
                            "This is a startup initialization failure.", symbol
                        )
                        if self.telegram_bot:
                            asyncio.create_task(self.telegram_bot.send_alert(
                                f"🚨 CRITICAL: OrderManager not initialized. "
                                f"Order for {symbol} BLOCKED. Check startup init chain."
                            ))
                        raise RuntimeError(f"OrderManager not initialized for {symbol}")

                    logger.info(f"🚀 [EXECUTING] {symbol} | trigger=₹{trigger_price} ltp=₹{ltp}")

                    # Execution cooldown gate
                    # order_manager._exec_cooldowns is set on any failed entry attempt.
                    # Signal is NOT removed from gate — stays observable for ML logging.
                    if self.order_manager and hasattr(self.order_manager, 'is_exec_cooldown_active'):
                        cd_active, cd_remaining = self.order_manager.is_exec_cooldown_active(symbol)
                        if cd_active:
                            logger.info(
                                f"⏳ EXEC COOLDOWN {symbol} | {cd_remaining}s remaining | "
                                f"signal visible but not executed"
                            )
                            if self.telegram_bot:
                                asyncio.create_task(self.telegram_bot.send_alert(
                                    f"⏳ *EXEC COOLDOWN ACTIVE*\n\n"
                                    f"Symbol: `{symbol}`\n"
                                    f"Trigger broke @ ₹{ltp:.2f}\n"
                                    f"Blocked: {cd_remaining}s remaining\n\n"
                                    f"_Signal valid — not executed due to cooldown_"
                                ))
                            # DO NOT delete from pending_signals — keep for continued monitoring
                            continue

                    pos = await self.order_manager.enter_position(pending['data'])
                    logger.info(f"[DEBUG] enter_position returned type={type(pos)} value={pos}")

                    if pos and isinstance(pos, dict):
                        try:
                            signal_data = pending.get('data', {})
                            if analyzer and hasattr(analyzer, 'signal_manager'):
                                sl      = signal_data.get('stop_loss', 0.0)
                                pattern = signal_data.get('pattern', '')
                                analyzer.signal_manager.record_signal(symbol, ltp, sl, pattern)
                                remaining = analyzer.signal_manager.get_remaining_signals()
                                logger.info(f"[SignalManager] Slot burned for {symbol}. Remaining today: {remaining}")
                        except Exception as _sm_err:
                            logger.warning(f"[SignalManager] record_signal failed (non-fatal): {_sm_err}")

                        if _gr is not None:
                            _gr.verdict     = "SIGNAL_FIRED"
                            _gr.entry_price = pos.get('entry_price') or pos.get('entry')
                            _gr.qty         = pos.get('qty')
                            get_gate_result_logger().record(_gr)
                        self.start_focus(symbol, pos)

                    else:
                        # No Telegram alert here — enter_position already sent one
                        # with the broker's real rejection text. A second message
                        # saying "broker returned None" once hid an RMS rule.
                        logger.warning(
                            "⚠️ [EXECUTION FAILED] %s — enter_position returned: %s",
                            symbol, pos,
                        )

                    del self.pending_signals[symbol]
                    continue
                    
                # Invalidation and timeout.
                elif ltp > inval_price:
                    logger.info(f"🚫 [INVALIDATED] {symbol} hit G12 tighter buffer {inval_price}")
                    _queue_validation_update(outcome='REJECTED', details={'reason': 'G12_INVALIDATED_BUFFER', 'ltp': ltp})
                    del self.pending_signals[symbol]
                    continue
                
                # Timeout
                elif datetime.datetime.now(pytz.timezone('Asia/Kolkata')) > pending.get('expires_at', datetime.datetime.now(pytz.timezone('Asia/Kolkata')) + datetime.timedelta(minutes=15)):
                    logger.info(f"⌛ [TIMEOUT] {symbol} expired at {pending.get('expires_at')}")
                    _queue_validation_update(outcome='TIMEOUT', details={'reason': 'G11_DYNAMIC_TIMEOUT'})
                    del self.pending_signals[symbol]
                    continue
                
                else:
                    pass
                     
            except Exception as e:
                logger.error(f"Validation Check Error {symbol}: {e}")
                # Alert on execution error instead of silent swallow
                if self.telegram_bot:
                    asyncio.create_task(self.telegram_bot.send_alert(
                        f"🔴 EXECUTION ERROR {symbol}: {e}"
                    ))
        
        return None

    def attempt_recovery(self):
        """
        Scan Fyers for open positions and resting orders to adopt orphaned trades.

        Previously this call could never succeed: it invoked
            start_focus(symbol, entry_price, sl_price, message_id=None, ...)
        against the signature
            start_focus(symbol, position_data, message_id=None, ...)
        so a float landed where a dict was expected and `.get()` raised on the first
        line. It also referenced `self.bot`, which does not exist (the attribute is
        `telegram_bot`). Both failures were swallowed by the outer except, so this
        logged "[RECOVERY] Failed" every startup and adopted nothing.
        """
        if run_mode.is_data_only():
            logger.info("🧪 [DATA-ONLY] recovery scan skipped — this mode adopts nothing")
            return
        try:
            logger.info("[RECOVERY] Scanning for orphaned trades...")
            positions = self.fyers.positions()

            if not isinstance(positions, dict) or 'netPositions' not in positions:
                logger.warning("[RECOVERY] Positions unavailable — skipping recovery scan.")
                return

            for p in positions['netPositions']:
                qty = p.get('netQty', 0) or 0
                if qty == 0:
                    continue

                symbol = p.get('symbol')
                if not symbol:
                    continue

                logger.info(f"[RECOVERY] Found Open Position: {symbol} Qty: {qty}")

                # Entry price: for a short the true entry is sellAvg, for a long buyAvg.
                if qty < 0:
                    entry_price = float(p.get('sellAvg') or p.get('netAvg') or p.get('avgPrice') or 0)
                else:
                    entry_price = float(p.get('buyAvg') or p.get('netAvg') or p.get('avgPrice') or 0)

                if entry_price <= 0:
                    logger.error(
                        "[RECOVERY] %s has no usable entry price (%s) — refusing to adopt "
                        "blind. Close manually or let reconciliation handle it.",
                        symbol, entry_price,
                    )
                    continue

                # Locate a resting stop for this symbol.
                is_short = qty < 0
                sl_price = entry_price * (1.01 if is_short else 0.99)  # conservative default
                try:
                    orders = self.fyers.orderbook()
                    for o in (orders or {}).get('orderBook', []):
                        if o.get('symbol') != symbol:
                            continue
                        # 6 = PENDING, 4 = TRANSIT; both are live resting orders.
                        if o.get('status') not in (4, 6):
                            continue
                        candidate = float(o.get('stopPrice') or 0) or float(o.get('limitPrice') or 0)
                        if candidate > 0:
                            sl_price = candidate
                            logger.info(f"[RECOVERY] Found resting SL order @ {sl_price}")
                            break
                except Exception as ob_err:
                    logger.warning(f"[RECOVERY] Orderbook lookup failed: {ob_err}")

                # start_focus expects a position_data DICT — this is the actual fix.
                position_data = {
                    'symbol':      symbol,
                    'entry_price': entry_price,
                    'stop_loss':   sl_price,
                    'qty':         abs(qty),
                    'tick_size':   0.05,
                    'trade_id_str': f"RECOVERY_{symbol}",
                }
                self.start_focus(symbol, position_data, message_id=None, trade_id="RECOVERY")

                if self.telegram_bot:
                    msg = (
                        f"♻️ *RECOVERY MODE*\n\n"
                        f"Adopted: `{symbol}`\n"
                        f"Qty: {abs(qty)} ({'SHORT' if is_short else 'LONG'})\n"
                        f"Entry: ₹{entry_price:.2f}\n"
                        f"SL: ₹{sl_price:.2f}\n\n"
                        f"_Bot is now monitoring this position._"
                    )
                    self._dispatch_alert(msg)

                # FocusEngine tracks one active trade.
                break

        except Exception as e:
            logger.error(f"[RECOVERY] Failed: {e}", exc_info=True)

    def _rsi_take_profit_due(self, symbol: str, t: dict):
        """
        The relief-rally exit for EOD_HOLD shorts: once the trade is in profit,
        cover when the 1-minute RSI closes below the threshold. Its parameters are
        the strategy's (ExitPlan.rsi_cover), carried on the position; a position
        without them never takes this exit.

        Returns the RSI reading when the exit should fire, else None. Evaluated once
        per COMPLETED 1-minute bar — a forming bar's RSI swings on every tick and
        the rule was measured on closes. Built from the broker's own tick-aggregated
        candles, so it costs no REST calls. Anything that cannot be read (no broker,
        too few candles for the RSI to have warmed up) leaves the position on its
        stop and the 15:10 square-off, exactly as before the rule existed.
        """
        rsi_cover = rsi_cover_of(t)
        if rsi_cover is None:
            return None
        if t.get('exit_profile') != 'EOD_HOLD' or t.get('direction', 'SHORT') != 'SHORT':
            return None
        # An exit already decided but not yet completed is retried every 10s,
        # not re-decided: the rule fired on a closed bar, and a bounce since then
        # is exactly what it was taking profit ahead of.
        retry_at = t.get('_rsi_retry_at')
        if retry_at is not None:
            return t.get('_rsi_reading') if time.time() >= retry_at else None

        broker = getattr(self.order_manager, 'broker', None) if self.order_manager else None
        if broker is None or not hasattr(broker, 'get_local_candles'):
            return None
        try:
            candles = broker.get_local_candles(symbol, 400)
        except Exception as e:
            logger.debug("[RSI-TP] %s candles unavailable: %s", symbol, e)
            return None

        now_minute = int(time.time() // 60 * 60)
        done = [c for c in candles if int(c.epoch) < now_minute]    # drop the forming bar
        if len(done) < rsi_cover.min_candles:
            return None
        last_epoch = int(done[-1].epoch)
        if t.get('_rsi_checked_epoch') == last_epoch:
            return None
        t['_rsi_checked_epoch'] = last_epoch

        entry_minute = int(t.get('entry_minute') or 0)
        bars_after_entry = sum(1 for c in done if int(c.epoch) >= entry_minute) - 1
        if bars_after_entry < rsi_cover.skip_minutes:
            return None
        if not float(done[-1].close) < float(t.get('entry') or 0):
            return None                           # not in profit: not a take-profit

        rsi = F.compute_rsi_wilder([c.close for c in done], rsi_cover.period)
        if rsi == rsi and rsi < rsi_cover.threshold:   # rsi == rsi rejects NaN
            return rsi
        return None

    def _staged_rsi_step(self, symbol: str, t: dict, ltp: float) -> Optional[str]:
        """
        One pass of the ExitProfile.STAGED_RSI exits.

        Returns None to carry on with the loop, 'retry' to go straight to the next
        tick, or 'done' when the trade has ended and the watch must stop.

        RSI is read once per completed bar, from the broker's tick-aggregated
        candles, so this costs no REST calls. If the RSI cannot be read (too few
        bars, no candles) nothing happens and the position stays on its hard stop
        and the square-off. A decision that fails is retried every 10s, never at
        5Hz: the usual cause of a failed order is a rate limit.
        """
        cfg = staged_rsi_of(t)
        if cfg is None or t.get('exit_profile') != 'STAGED_RSI' \
                or t.get('direction', 'SHORT') != 'SHORT':
            return None
        if not (self.order_manager and self._event_loop):
            return None
        broker = getattr(self.order_manager, 'broker', None)
        if broker is None or not hasattr(broker, 'get_local_candles'):
            return None

        st = t.setdefault('staged', {'scaled_out': False, 'trailing': False,
                                     'rsi_epoch': None, 'trail_epoch': None})
        now = time.time()
        if now < st.get('retry_at', 0.0):
            return None
        try:
            candles = broker.get_local_candles(symbol, 400)
        except Exception as e:
            logger.debug("[STAGED] %s candles unavailable: %s", symbol, e)
            return None

        bars = completed_bars(candles, cfg.bar_minutes, now)
        if len(bars) >= cfg.min_bars and bars[-1].epoch != st.get('rsi_epoch'):
            last = bars[-1]
            st['rsi_epoch'] = last.epoch
            rsi = F.compute_rsi_wilder([b.close for b in bars], cfg.period)
            # A bar that closed before the entry describes the setup, not the trade.
            closed_after_entry = last.epoch + cfg.bar_minutes * 60 > float(t.get('entry_ts') or 0)
            if rsi == rsi and closed_after_entry:                # rsi == rsi rejects NaN
                in_profit = last.close < t['entry'] and 0 < ltp < t['entry']
                if not st['scaled_out'] and rsi <= cfg.scale_out_rsi and in_profit:
                    if self._staged_scale_out(symbol, t, cfg, rsi) == 'done':
                        return 'done'
                if st['scaled_out'] and not st['trailing'] and rsi <= cfg.trail_rsi:
                    st['trailing'] = True
                    logger.info(
                        "🪢 [STAGED] %s %s-min RSI %.1f ≤ %.0f — trailing the stop on "
                        "the last %s shares above each completed %s-min high",
                        symbol, cfg.bar_minutes, rsi, cfg.trail_rsi,
                        t['remaining_qty'], cfg.trail_bar_minutes,
                    )

        if st['trailing']:
            return self._staged_trail(symbol, t, cfg, candles, ltp, now)
        return None

    def _staged_scale_out(self, symbol: str, t: dict, cfg: StagedRsiExit, rsi: float) -> Optional[str]:
        st = t['staged']
        remaining = int(t['remaining_qty'])
        exit_qty = int(remaining * cfg.scale_out_fraction)
        # Latched before dispatch, so a slow broker cannot let the next tick fire
        # a second partial against the same position.
        st['scaled_out'] = True

        if exit_qty <= 0:
            logger.info("[STAGED] %s has %s share(s) — nothing to scale out", symbol, remaining)
            if cfg.stop_to_cost and not self._resize_stop_after_partial(symbol, t, stop_to_cost=True):
                return 'done'
            return None

        logger.info(
            "🎯 [STAGED] %s %s-min RSI %.1f ≤ %.0f in profit — booking %s of %s",
            symbol, cfg.bar_minutes, rsi, cfg.scale_out_rsi, exit_qty, remaining,
        )
        filled = False
        try:
            filled = asyncio.run_coroutine_threadsafe(
                self.order_manager.partial_exit(symbol, exit_qty, "RSI_SCALE_OUT"),
                self._event_loop,
            ).result(timeout=25)
        except Exception as pe:
            logger.error("[STAGED] %s partial_exit raised: %s", symbol, pe)

        if not filled:
            # Nothing sold: position and stop still agree. Re-decide on a later pass.
            st['scaled_out'] = False
            st['rsi_epoch'] = None
            st['retry_at'] = time.time() + 10.0
            logger.warning("[STAGED] %s scale-out did not fill — position unchanged, "
                           "retrying in 10s", symbol)
            return None

        t['remaining_qty'] -= exit_qty
        if not self._resize_stop_after_partial(symbol, t, stop_to_cost=cfg.stop_to_cost):
            return 'done'
        self._dispatch_alert(
            f"🎯 *SCALE-OUT*\n\n`{symbol}` RSI {rsi:.1f} — booked {exit_qty}, "
            f"{t['remaining_qty']} left with the stop at "
            f"{'cost' if t.get('be_moved') else 'its original level'}."
        )
        return None

    def _staged_trail(self, symbol: str, t: dict, cfg: StagedRsiExit,
                      candles, ltp: float, now: float) -> Optional[str]:
        st = t['staged']
        bars = completed_bars(candles, cfg.trail_bar_minutes, now)
        if not bars or bars[-1].epoch == st.get('trail_epoch'):
            return None
        last = bars[-1]
        st['trail_epoch'] = last.epoch

        tick = float(t.get('tick_size') or 0.05)
        new_stop = OrderManager._round_sl_to_tick(
            last.high * (1 + cfg.trail_buffer_pct / 100.0), 'SELL', tick
        )
        current = float(t.get('sl') or 0)
        if current and new_stop >= current:
            return None                                  # a trail only ever tightens

        if ltp >= new_stop:
            # Price is already back above the level the trail would use, so the
            # trail is hit. A stop placed there would be marketable on arrival.
            logger.info("🪢 [STAGED] %s ltp ₹%.2f is above the trail level ₹%.2f — closing",
                        symbol, ltp, new_stop)
            exited = False
            try:
                exited = asyncio.run_coroutine_threadsafe(
                    self.order_manager.safe_exit(symbol, "TRAIL_STOP"), self._event_loop,
                ).result(timeout=30)
            except Exception as err:
                logger.error("[STAGED] %s trail exit failed: %s", symbol, err)
            if self._retire_or_retry(symbol, exited, "TRAIL_STOP"):
                return 'done'
            st['trail_epoch'] = None
            st['retry_at'] = time.time() + 10.0
            return 'retry'

        ok = False
        try:
            ok = asyncio.run_coroutine_threadsafe(
                self.order_manager.move_hard_stop(symbol, new_stop, new_qty=t['remaining_qty']),
                self._event_loop,
            ).result(timeout=15)
        except Exception as err:
            logger.error("[STAGED] %s trail move raised: %s", symbol, err)
        if ok:
            logger.info("🪢 [STAGED] %s stop trailed ₹%.2f → ₹%.2f x%s",
                        symbol, current, new_stop, t['remaining_qty'])
            t['sl'] = new_stop
        else:
            # The previous stop is still resting, so the position is protected.
            st['trail_epoch'] = None
            st['retry_at'] = time.time() + 10.0
            logger.warning("[STAGED] %s trail to ₹%.2f not accepted — stop stays at ₹%.2f, "
                           "retrying in 10s", symbol, new_stop, current)
        return None

    def _retire_or_retry(self, symbol: str, exited: bool, reason: str) -> bool:
        """
        True to stop watching this position, False to keep the loop alive.

        `safe_exit` can now legitimately decline — it refuses to cancel a stop on
        a position the broker will not confirm. Treating that as done would strand
        a live position with its exit level already breached and nothing left
        watching it, which is a worse outcome than the bug it replaces.

        Retries are throttled: the focus loop runs at 5Hz and the failure this
        handles is a rate limit, so hammering it is what caused the problem.
        """
        if exited:
            self.stop_focus(reason)
            return True

        now = time.time()
        last = self._exit_retry_at.get(symbol, 0.0)
        if now - last < 10.0:
            time.sleep(0.2)
            return False

        self._exit_retry_at[symbol] = now
        attempts = self._exit_retry_count.get(symbol, 0) + 1
        self._exit_retry_count[symbol] = attempts
        logger.warning(
            "[EXIT-RETRY] %s %s did not complete (attempt %d) — still watching.",
            symbol, reason, attempts,
        )
        if attempts == 3:
            self._dispatch_alert(
                f"⚠️ *EXIT STILL PENDING*\n\n"
                f"Symbol: `{symbol}`\n"
                f"Reason: `{reason}`\n\n"
                f"Three attempts have not completed. The stop is still in place and "
                f"the bot is still watching, but check your broker app."
            )
        return False

    def _dispatch_alert(self, message: str) -> None:
        """
        Send a Telegram alert from either sync or async context.

        attempt_recovery() runs during __init__, before any event loop exists, so a
        bare asyncio.create_task() there raises. Resolve the context at call time.
        """
        bot = self.telegram_bot
        if not bot:
            return
        try:
            loop = getattr(self, '_event_loop', None)
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(bot.send_alert(message), loop)
                return
            try:
                running = asyncio.get_running_loop()
                running.create_task(bot.send_alert(message))
            except RuntimeError:
                # No loop yet (startup path) — queue for the morning briefing instead
                # of crashing the recovery scan.
                pending = getattr(self, '_deferred_alerts', None)
                if pending is None:
                    pending = self._deferred_alerts = []
                pending.append(message)
                logger.info("[ALERT] Deferred (no event loop yet): %s", message[:60])
        except Exception as e:
            logger.warning(f"[ALERT] Dispatch failed: {e}")

    def _resize_stop_after_partial(self, symbol: str, t: dict,
                                   stop_to_cost: Optional[bool] = None) -> bool:
        """Shrink the broker-side stop to match what is left after a partial fill.

        Returns True if the trade is still open, False if the remainder was closed.

        This is a safety invariant, not a feature, and it is deliberately NOT gated on
        P52_BREAKEVEN_AFTER_TP1. On 2026-09-01 the two were the same code path: the
        breakeven move failed, so nothing resized the stop, and a BUY x10 stop rested
        against a short of 5. When it triggered it bought 10, flipping the account
        LONG 5 — reconciliation then adopted that as a manual entry.

        Breakeven only decides the stop PRICE. The quantity is corrected either way,
        and if it cannot be corrected the remainder is closed rather than left under
        an oversized stop.

        `stop_to_cost` is the strategy's own breakeven choice (STAGED_RSI); None
        defers to P52_BREAKEVEN_AFTER_TP1, the TP-1 ladder's switch.
        """
        new_qty = t['remaining_qty']
        be_enabled = (getattr(config, 'P52_BREAKEVEN_AFTER_TP1', False)
                      if stop_to_cost is None else bool(stop_to_cost))
        target_price = t['entry'] if be_enabled else t['sl']
        t['be_moved'] = False

        # Real backoff. The old retry fired both attempts 51ms apart, which against a
        # rate limit is one attempt with extra logging.
        for attempt, delay in ((1, 0.0), (2, 1.5), (3, 4.0)):
            if delay:
                time.sleep(delay)
            try:
                ok = asyncio.run_coroutine_threadsafe(
                    self.order_manager.move_hard_stop(symbol, target_price, new_qty=new_qty),
                    self._event_loop,
                ).result(timeout=15)
            except Exception as err:
                logger.error("[SL_RESIZE] %s attempt %d raised: %s", symbol, attempt, err)
                continue
            if not ok:
                logger.error("[SL_RESIZE] %s attempt %d returned False", symbol, attempt)
                continue

            # 's': 'ok' is the broker accepting the request, not proof the resting
            # order changed. Assert the invariant against the orderbook itself.
            try:
                resting = asyncio.run_coroutine_threadsafe(
                    self.order_manager.verify_stop_qty(symbol), self._event_loop,
                ).result(timeout=15)
            except Exception as err:
                logger.warning("[SL_RESIZE] %s verify raised: %s", symbol, err)
                resting = None

            if resting is not None and resting > new_qty:
                logger.error(
                    "[SL_RESIZE] %s broker accepted the modify but the resting stop is "
                    "still %s against a position of %s — retrying",
                    symbol, resting, new_qty,
                )
                continue

            t['be_moved'] = be_enabled
            t['sl'] = target_price
            logger.info(
                "🔒 [SL_RESIZE] %s stop now ₹%.2f x%s (verified=%s)%s",
                symbol, target_price, new_qty,
                'unavailable' if resting is None else resting,
                " — remainder is risk-free" if be_enabled else "",
            )
            return True

        # Could not guarantee the stop matches the position. An oversized stop is how
        # a short becomes a long, so close the remainder instead of carrying it.
        logger.critical(
            "🚨 [SL_RESIZE] %s could not resize the stop to %s after 3 attempts — "
            "flattening the remainder rather than leaving an oversized stop resting.",
            symbol, new_qty,
        )
        # A STAGED_RSI partial has no TP-1 level; formatting None would raise
        # here, on the path that is meant to flatten an oversized stop.
        partial_at = f" at ₹{t['tp_1']:.2f}" if t.get('tp_1') else ""
        self._dispatch_alert(
            f"🚨 *STOP RESIZE FAILED*\n\n"
            f"Symbol: `{symbol}`\n"
            f"Took the partial{partial_at}, but the broker stop could not be "
            f"reduced to {new_qty}.\n\n"
            f"Closing the remainder now — an oversized stop would flip the position."
        )
        try:
            asyncio.run_coroutine_threadsafe(
                self.order_manager.safe_exit(symbol, "SL_RESIZE_FAILED"),
                self._event_loop,
            ).result(timeout=30)
        except Exception as err:
            logger.critical(
                "🚨 [SL_RESIZE] %s flatten also failed: %s — MANUAL INTERVENTION NEEDED",
                symbol, err,
            )
            self._dispatch_alert(
                f"🚨🚨 *MANUAL ACTION NEEDED*\n\n`{symbol}` has an oversized stop "
                f"resting and the automatic close failed. Close it by hand now."
            )
            return False
        self.stop_focus("SL_RESIZE_FAILED")
        return False

    def start_focus(self, symbol, position_data, message_id=None, trade_id=None, qty=1):
        """
        Latch onto a trade. Phase 94: Direction-aware.
        """
        entry_price = position_data.get('entry_price', position_data.get('entry', 0))
        sl_price = position_data.get('stop_loss',
                       position_data.get('hard_stop_price',
                           position_data.get('sl', 0)))
        actual_qty = position_data.get('qty', qty)

        # Read direction from shortcircuit.config
        direction = config.TRADE_DIRECTION  # 'SHORT' or 'LONG'
        is_long = direction == 'LONG'

        # Soft SL: opposite side from entry
        # Provide fallback if DISCRETIONARY_CONFIG was removed
        soft_stop_pct = getattr(config, 'DISCRETIONARY_CONFIG', {}).get('soft_stop_pct', 0.015)

        exit_profile = str(position_data.get('exit_profile') or 'DEFAULT').upper()
        # Both non-default profiles run without the VWAP ladder, and their hard
        # stop — not the 1.5% soft stop — is the one that governs.
        eod_hold = exit_profile in ('EOD_HOLD', 'STAGED_RSI')

        if is_long:
            soft_sl = entry_price * (1 - soft_stop_pct)
        else:
            soft_sl = entry_price * (1 + soft_stop_pct)

        # A non-default profile's hard stop is the strategy's measured risk, and
        # is often wider than the 1.5% soft stop (median 1.63% on the setups
        # EOD_HOLD was measured on). Left inside it, the soft stop would quietly
        # exit many trades early, which is not the strategy that was tested.
        # Push it just past the hard stop so it still backstops a broker stop
        # that never fired, but never governs the exit.
        if eod_hold and sl_price > 0:
            if is_long:
                soft_sl = min(soft_sl, sl_price * 0.999)
            else:
                soft_sl = max(soft_sl, sl_price * 1.001)
            logger.info(
                "%s %s %s — soft stop moved to ₹%.2f, behind the hard stop at ₹%.2f",
                f"[{exit_profile}]",
                symbol, exit_profile, soft_sl, sl_price,
            )

        tick = position_data.get('tick_size', 0.05)

        # tp_2 is the VWAP target from OrderManager; tp_1 the midpoint to it.
        # SCALE sheds 50% at tp_1 and runs the rest to tp_2, SINGLE closes fully
        # at tp_1. Both are None under 'OFF', and None means "no target".
        tp_mode = str(getattr(config, 'TP_MODE', 'OFF')).upper()
        if eod_hold:
            # Not a tuning choice: across 206 trades the same entries scored
            # +0.09% held to the square-off and -0.21% under TP1/breakeven/TP2.
            tp_mode = 'OFF'
        tp_1 = tp_2 = None
        if tp_mode in ('SCALE', 'SINGLE'):
            tps = {}
            if self.order_manager and entry_price > 0:
                try:
                    tps = self.order_manager.compute_take_profits(entry_price, position_data)
                except Exception as tp_err:
                    logger.error("[TP] compute_take_profits failed for %s: %s", symbol, tp_err)
            tp_default = entry_price * (1.01 if is_long else 0.99)
            tp_1, tp_2 = compute_tp_levels(
                entry_price, tps.get('tp', tp_default), tick, is_long
            )
            if tp_1 is None:
                logger.warning(
                    "[TP] %s VWAP target is not beyond entry ₹%.2f — running without a TP",
                    symbol, entry_price,
                )

        self.active_trade = {
            'symbol':          symbol,
            'entry':           entry_price,
            'sl':              sl_price,
            'soft_sl':         soft_sl,
            'tp_mode':         tp_mode,
            'tp_1':            tp_1,       # midpoint — partial (SCALE) or full (SINGLE) exit
            'tp':              tp_2,       # VWAP target — final exit under SCALE
            'tp_1_hit':        False,      # state tracker for the partial
            'be_moved':        False,      # did the breakeven stop actually land?
            'status':          'OPEN',
            'highest_profit':  -999,
            'message_id':      message_id,
            'trade_id':        (
                position_data.get('trade_id_str')
                if isinstance(position_data, dict) and position_data.get('trade_id_str')
                else (trade_id or f"Trd_{int(time.time())}")
            ),
            'last_price':      entry_price,
            'qty':             actual_qty,
            'remaining_qty':   actual_qty,
            'start_time':      time.time(),
            'direction':       direction,  # Store direction for TP/BE/PnL logic
            
            # MFE/MAE tracking for the ML trainer.
            'mfe_pct':         0.0,  # Max Favorable Excursion (% from entry)
            'mae_pct':         0.0,  # Max Adverse Excursion (% from entry)

            # Read by the RSI take-profit: which exit rules apply, and from which
            # 1-minute bar the position has existed.
            'exit_profile':    exit_profile,
            'exit_params':     position_data.get('exit_params'),
            'entry_minute':    int(time.time() // 60 * 60),
            'entry_ts':        time.time(),
            'tick_size':       tick,
            # STAGED_RSI progress. Latched before each action is dispatched, and
            # unlatched only if the action provably did not happen.
            'staged':          {'scaled_out': False, 'trailing': False,
                                'rsi_epoch': None, 'trail_epoch': None},
        }
        
        # Per-trade detector state MUST be reset here — it lives on the engine,
        # not the trade. On 2026-08-06 STOVEKRAFT left _consecutive_flat_reads at 1,
        # so the next trade needed a single flat read to hit the "2 consecutive"
        # threshold and was declared manually closed 23ms after entry.
        self._consecutive_flat_reads = 0
        self._api_fail_streak = 0
        self._last_broker_pos_check = time.time()

        # Grace period: give the broker time to register the fill and push its first
        # position frame before any close-detection is allowed to run at all.
        self._flat_check_not_before = time.time() + 10.0

        self.is_running = True
        if tp_1 is None:
            _tp_desc = "no TP — runs to SL or EOD"
        elif tp_mode == 'SINGLE':
            _tp_desc = f"tp=₹{tp_1:.2f} (100% at midpoint)"
        else:
            _tp_desc = f"tp1=₹{tp_1:.2f} (50%) tp2=₹{tp_2:.2f}"
        logger.info(
            f"[FOCUS] Started {symbol} qty={actual_qty} entry=₹{entry_price:.2f} "
            f"sl=₹{sl_price:.2f} {_tp_desc}"
        )
        
        self.thread = threading.Thread(target=self.focus_loop, daemon=True)
        self.thread.start()

    def _check_broker_position(self, symbol: str) -> dict:
        """
        Query broker for the current position.

        Returns:
          - Position dict if found
          - None if the source was authoritative and the position is genuinely gone
          - {'_api_failed': True} if we could not determine the truth (NEVER flat)

        The websocket position cache is consulted first. That cache is now actually
        populated (it previously had no writer), so the common case costs zero REST
        calls instead of one every 5 seconds per open position.

        A cache MISS is deliberately not treated as flat — the cache only holds
        symbols that have pushed an event this session, so absence proves nothing.
        We fall through to REST for that verdict.
        """
        broker = self.order_manager.broker if self.order_manager else None

        # Fast path: live WS position cache. A HIT is authoritative; a MISS is
        # NOT — it only means no frame has arrived for this symbol yet. An earlier
        # version inferred "flat" from a miss whenever ANY symbol had pushed an
        # event recently, and on 2026-08-06 declared NSE:BAJAJELEC-EQ manually
        # closed 23ms after entry, 540ms before its own first frame arrived.
        if broker is not None:
            try:
                with broker._position_cache_lock:
                    entry = broker.position_cache.get(symbol)

                if entry is not None:
                    age = (datetime.datetime.now(datetime.timezone.utc) - entry.timestamp).total_seconds()
                    if age < broker.POSITION_CACHE_TTL_SECONDS:
                        return {
                            'symbol': symbol,
                            'netQty': entry.net_qty,
                            'qty': abs(entry.net_qty),
                            'avgPrice': entry.avg_price,
                            '_source': 'ws_cache',
                        }
                # Miss → fall through to REST. Never infer "flat" from absence.
            except Exception as e:
                logger.debug(f"[SAFETY] WS position cache read failed: {e}")

        # Authoritative path: REST, metered at HIGH by the transport. This runs
        # from the 5Hz focus loop on every cache miss; the loop's own back-off
        # keeps it to one read every 5-30s.
        try:
            positions = self.fyers.positions()
            if not isinstance(positions, dict) or positions.get('s') != 'ok' \
                    or 'netPositions' not in positions:
                logger.error("[SAFETY] Could not fetch positions (API error, not flat)")
                return {'_api_failed': True}

            for pos in positions.get('netPositions', []):
                if pos.get('symbol') == symbol:
                    return pos

            return None  # API succeeded, position genuinely not found = closed

        except Exception as e:
            logger.error(f"[SAFETY] Broker position check failed: {e}")
            return {'_api_failed': True}

    def _hard_stop_poll_due(self) -> bool:
        """
        Whether the focus loop may dispatch another hard-stop check now.

        Each check reads the full orderbook over REST. Dispatched on every 5 Hz
        tick, fire-and-forget, it was ~280 requests a minute against Fyers'
        200/min limit: on 2026-09-23 and 2026-10-13 the account was throttled
        within a minute of every entry, and the throttle also failed the reads
        that verify exits. One check in flight, at most every
        HARD_STOP_POLL_INTERVAL_S, is ~30 a minute. The stop itself rests at the
        exchange, so this paces only how soon the bot notices a fill — and every
        bot exit re-verifies the position at the broker before acting.
        """
        pending = getattr(self, '_hard_stop_future', None)
        if pending is not None and not pending.done():
            return False
        now = time.monotonic()
        if now - getattr(self, '_last_hard_stop_poll', float('-inf')) < HARD_STOP_POLL_INTERVAL_S:
            return False
        self._last_hard_stop_poll = now
        return True

    def focus_loop(self):
        while self.is_running and self.active_trade:
            try:
                symbol = self.active_trade['symbol']

                # Safety: has the position been closed externally?
                if self.order_manager:
                    om_pos = self.order_manager.active_positions.get(symbol)
                    if not om_pos or om_pos['status'] != 'OPEN':
                         logger.info(f"[FOCUS] Position closed in OrderManager. Stopping Focus.")
                         self.stop_focus("CLOSED_EXTERNALLY")
                         return
                    
                    # monitor_hard_stop_status is async — dispatch from this thread,
                    # but at most one in flight and no more often than the interval.
                    if self._event_loop and self._hard_stop_poll_due():
                        self._hard_stop_future = asyncio.run_coroutine_threadsafe(
                            self.order_manager.monitor_hard_stop_status(symbol),
                            self._event_loop
                        )

                # Manual-override check
                manual_override = False
                _eod_hold = False
                if self.order_manager:
                    pos = self.order_manager.active_positions.get(symbol, {})
                    manual_override = pos.get('manual_override', False)
                    # STAGED_RSI is exempt for the same reason: its exits are
                    # RSI-timed, and a clock would cut them short.
                    _eod_hold = str(
                        pos.get('exit_profile') or 'DEFAULT'
                    ).upper() in ('EOD_HOLD', 'STAGED_RSI')

                # Time-based stop: the mean-reversion thesis has expired.
                # It does not apply to EOD_HOLD, whose thesis is distribution into
                # the close and whose measured edge comes from the trades that take
                # longer than 45 minutes to work. The 15:10 square-off still ends it.
                _max_hold = getattr(config, 'MAX_HOLD_TIME_MINUTES', 0) or 0
                if not manual_override and not _eod_hold and _max_hold > 0 and self.order_manager:
                    om_pos = self.order_manager.active_positions.get(symbol)
                    if om_pos and om_pos.get('status') == 'OPEN' and 'entry_time' in om_pos:
                        hold_duration = (datetime.datetime.now() - om_pos['entry_time']).total_seconds() / 60.0
                        if hold_duration >= _max_hold:
                            logger.warning(f"⏰ [TIME_STOP] {symbol} held for {hold_duration:.1f} mins > {_max_hold} mins limit. Exiting.")
                            if self._event_loop:
                                future = asyncio.run_coroutine_threadsafe(
                                    self.order_manager.safe_exit(symbol, "TIME_STOP"),
                                    self._event_loop
                                )
                                try:
                                    future.result(timeout=15)
                                except Exception as ts_err:
                                    logger.error(f"[TIME_STOP] safe_exit failed for {symbol}: {ts_err}")
                            self.stop_focus("TIME_STOP")
                            return

                # EOD square-off: 15:10, or the closing-auction cutoff for an F&O
                # stock, which the broker squares off itself at 15:05. IST
                # explicitly — the container clock is UTC.
                if squareoff.past_squareoff(symbol):
                    logger.warning(
                        "⏰ [EOD] Force Closing %s at %s", symbol,
                        squareoff.squareoff_time(symbol).strftime("%H:%M"),
                    )
                    if self.order_manager and self._event_loop:
                        future = asyncio.run_coroutine_threadsafe(
                            self.order_manager.safe_exit(symbol, "EOD_SQUARE_OFF"),
                            self._event_loop
                        )
                        try:
                            result = future.result(timeout=30)
                            logger.info(f"[EOD] safe_exit completed for {symbol}: success={result}")
                        except Exception as eod_err:
                            logger.error(f"[EOD] safe_exit failed for {symbol}: {eod_err}")
                    self.stop_focus("EOD")
                    return

                # 1. Fetch Price from WebSocket Cache (0ms latency, high frequency)
                ltp = 0
                if self.order_manager and self.order_manager.broker:
                    snapshot = self.order_manager.broker.get_quote_cache_snapshot()
                    if symbol in snapshot:
                        ltp = snapshot[symbol]['ltp']
                
                # Fallback to REST only if cache miss
                if ltp == 0:
                    data = {"symbols": symbol}
                    response = self.fyers.quotes(data=data)
                    if 'd' in response and len(response['d']) > 0:
                        quote = response['d'][0]
                        qt = quote.get('v', quote)
                        ltp = qt.get('lp')

                if not ltp:
                    time.sleep(1)
                    continue

                self.active_trade['last_price'] = ltp
                t = self.active_trade

                # Track MFE/MAE on every tick
                _entry = t['entry']
                if _entry > 0:
                    _tdir = t.get('direction', 'SHORT')
                    if _tdir == 'LONG':
                        # LONG: favorable = price going UP, adverse = price going DOWN
                        fav_pct = ((ltp - _entry) / _entry) * 100
                        adv_pct = ((_entry - ltp) / _entry) * 100
                    else:
                        # SHORT: favorable = price going DOWN, adverse = price going UP
                        fav_pct = ((_entry - ltp) / _entry) * 100
                        adv_pct = ((ltp - _entry) / _entry) * 100
                    t['mfe_pct'] = max(t.get('mfe_pct', 0), fav_pct)
                    t['mae_pct'] = max(t.get('mae_pct', 0), adv_pct)

                    # Sync to order_manager for ML logging on close
                    if self.order_manager and symbol in self.order_manager.active_positions:
                        self.order_manager.active_positions[symbol]['mfe_pct'] = t['mfe_pct']
                        self.order_manager.active_positions[symbol]['mae_pct'] = t['mae_pct']

                # Manual-close detection, broker-side.
                # Every ~5 seconds, check if the broker still has this position.
                # If not, the user closed it manually via the app.
                # SAFETY: Require 2 consecutive CONFIRMED flat reads to avoid
                # false positives from transient API failures.
                # Exponential backoff on API failures to avoid rate-limit storms.
                _last_broker_check = getattr(self, '_last_broker_pos_check', 0)
                _api_fail_streak = getattr(self, '_api_fail_streak', 0)
                _check_interval = min(5 * (2 ** _api_fail_streak), 30)  # 5s → 10s → 20s → 30s max
                # Post-entry grace: the broker needs a moment to register the fill.
                # Without this, close-detection can race the position's own first frame.
                _grace_until = getattr(self, '_flat_check_not_before', 0)
                if time.time() < _grace_until:
                    pass
                elif time.time() - _last_broker_check > _check_interval:
                    self._last_broker_pos_check = time.time()
                    broker_pos = self._check_broker_position(symbol)

                    # If API failed, skip — do NOT treat as flat
                    if isinstance(broker_pos, dict) and broker_pos.get('_api_failed'):
                        self._api_fail_streak = _api_fail_streak + 1
                        if self._api_fail_streak <= 3:  # Only log first few to avoid spam
                            logger.debug(f"[FOCUS] Broker API failed for {symbol} — backoff to {min(5 * (2 ** self._api_fail_streak), 30)}s")
                        self._consecutive_flat_reads = 0
                        pass  # Continue monitoring

                    elif broker_pos is None or broker_pos.get('netQty', 0) == 0:
                        # Confirmed flat — increment counter (API succeeded, reset backoff)
                        self._api_fail_streak = 0
                        self._consecutive_flat_reads = getattr(self, '_consecutive_flat_reads', 0) + 1
                        if self._consecutive_flat_reads < 2:
                            logger.info(f"[FOCUS] Broker flat read #{self._consecutive_flat_reads} for {symbol} — waiting for confirmation")
                        else:
                            logger.warning(
                                f"👻 [FOCUS] Broker CONFIRMED FLAT for {symbol} (2 reads) — manual close detected! "
                                f"Releasing slot and stopping focus."
                            )

                            # Compute PnL from entry and last known price (direction-aware)
                            entry_p = t.get('entry', 0)
                            exit_p = ltp
                            qty = t.get('qty', 0)
                            _dir = t.get('direction', 'SHORT')
                            if _dir == 'LONG':
                                pnl = (exit_p - entry_p) * qty if entry_p > 0 else 0
                            else:
                                pnl = (entry_p - exit_p) * qty if entry_p > 0 else 0
                            # This branch only sees that the broker position
                            # vanished. Past the square-off deadline that is the
                            # scheduler's doing, not the operator's, and calling it
                            # a manual close corrupts exit-reason attribution.
                            from shortcircuit.eod.eod_scheduler import EOD_TIME
                            _now_ist = datetime.datetime.now(
                                pytz.timezone('Asia/Kolkata')
                            ).time()
                            _is_eod_window = _now_ist >= EOD_TIME
                            _close_label = (
                                "🌆 **EOD SQUARE-OFF CLOSED**" if _is_eod_window
                                else "👻 **MANUAL CLOSE DETECTED**"
                            )
                            logger.info(
                                f"💰 [{'EOD EXIT' if _is_eod_window else 'MANUAL EXIT'}] "
                                f"{symbol} {_dir} | Entry ₹{entry_p:.2f} → Exit ~₹{exit_p:.2f} | "
                                f"PnL ≈ ₹{pnl:.2f}"
                            )

                            # Sync internal state: mark closed
                            if self.order_manager and symbol in self.order_manager.active_positions:
                                self.order_manager.active_positions[symbol]['status'] = 'CLOSED'

                            if self.order_manager and self._event_loop:
                                try:
                                    future = asyncio.run_coroutine_threadsafe(
                                        self.order_manager._finalize_closed_position(
                                            symbol=symbol,
                                            reason=('EOD_SQUAREOFF' if _is_eod_window
                                                    else 'MANUAL_CLOSE_DETECTED'),
                                            exit_price=exit_p,
                                            pnl=pnl,
                                            send_alert=True
                                        ),
                                        self._event_loop
                                    )
                                    future.result(timeout=20)
                                except Exception as e:
                                    # concurrent.futures.TimeoutError stringifies to
                                    # '', which produced a blank error in the log.
                                    logger.error(
                                        "[FOCUS] _finalize_closed_position failed: %s: %s",
                                        type(e).__name__, e or "(timed out)",
                                    )
                                    # Fallback: release capital directly
                                    if self.order_manager.capital:
                                        asyncio.run_coroutine_threadsafe(
                                            self.order_manager.capital.release_slot(broker=self.order_manager.broker),
                                            self._event_loop
                                        )

                            if self.telegram_bot and self._event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    self.telegram_bot.send_alert(
                                        f"{_close_label}\n\n"
                                        f"Symbol: `{symbol}`\n"
                                        f"Entry: ₹{entry_p:.2f}\n"
                                        f"Exit: ~₹{exit_p:.2f}\n"
                                        f"PnL: ₹{pnl:.2f}\n\n"
                                        f"✅ Capital slot released.\n"
                                        f"✅ Bot state synced."
                                    ),
                                    self._event_loop
                                )

                            self.stop_focus(
                                "EOD_SQUAREOFF" if _is_eod_window else "MANUAL_CLOSE"
                            )
                            return

                    else:
                        # Position exists on broker — reset flat counter & backoff
                        self._consecutive_flat_reads = 0
                        self._api_fail_streak = 0


                # No standalone breakeven stop by design: the profit-triggered
                # version was removed on 2026-08-12 as an operator-only decision.
                # The only one left is tied to the scale-out partial below.
                
                # RSI relief-rally take-profit (EOD_HOLD shorts). Respects
                # manual_override like every other bot-initiated exit.
                _rsi_hit = None if manual_override else self._rsi_take_profit_due(symbol, t)
                if _rsi_hit is not None:
                    logger.info(
                        "🎯 [RSI-TP] %s 1-min RSI %.1f < %.0f while in profit "
                        "(entry ₹%.2f, ltp ₹%.2f) — covering %s shares",
                        symbol, _rsi_hit,
                        rsi_cover_of(t).threshold,
                        t['entry'], ltp, t['remaining_qty'],
                    )
                    if self.order_manager and self._event_loop:
                        exited = False
                        future = asyncio.run_coroutine_threadsafe(
                            self.order_manager.safe_exit(symbol, "RSI_TP"), self._event_loop
                        )
                        try:
                            exited = future.result(timeout=30)
                            logger.info("[RSI-TP] safe_exit completed for %s: success=%s",
                                        symbol, exited)
                        except Exception as rsi_exit_err:
                            logger.error("[RSI-TP] safe_exit failed/timed out for %s: %s",
                                         symbol, rsi_exit_err)
                        if not self._retire_or_retry(symbol, exited, "RSI_TP"):
                            # Keep the decision, retry it every 10s — not at 5Hz,
                            # which is how a rate limit gets hit in the first place.
                            t['_rsi_reading'] = _rsi_hit
                            t['_rsi_retry_at'] = time.time() + 10.0
                            continue
                    else:
                        self.stop_focus("RSI_TP")
                    return

                # STAGED_RSI: scale-out to cost, then a trailing stop. Respects
                # manual_override like every other bot-initiated exit.
                _staged = None if manual_override else self._staged_rsi_step(symbol, t, ltp)
                if _staged == 'done':
                    return
                if _staged == 'retry':
                    continue

                # Take-profit engine — see config.TP_MODE for the mode ranking.
                # Both stages respect manual_override: once the operator has taken
                # the wheel, the bot does not exit underneath them.
                _tp_mode = t.get('tp_mode', 'OFF')
                _tp_dir = t.get('direction', 'SHORT')

                # Which level closes the position outright:
                #   SINGLE — the midpoint, 100%.
                #   SCALE  — the VWAP target, closing whatever the partial left.
                _full_exit_level = t.get('tp_1') if _tp_mode == 'SINGLE' else t.get('tp')

                # Stage 1: the full exit, checked FIRST
                # Order matters. A fast mover can gap through the midpoint and the
                # VWAP target inside one 5Hz tick. Taking the partial first would
                # dispatch partial_exit(half) and safe_exit(remaining) into the
                # loop together — for a short both are BUYs, so 1.5x gets bought
                # and the position flips net long. Checking the far level first
                # makes the two branches mutually exclusive, and the trade closes
                # at the better price anyway.
                if (not manual_override and _tp_mode in ('SINGLE', 'SCALE')
                        and target_reached(ltp, _full_exit_level, _tp_dir)):
                    logger.info(
                        "🎯 [TP] %s hit ₹%.2f — closing %s shares",
                        symbol, _full_exit_level, t['remaining_qty'],
                    )
                    if self.order_manager and self._event_loop:
                        exited = False
                        future = asyncio.run_coroutine_threadsafe(
                            self.order_manager.safe_exit(symbol, "TP_HIT"), self._event_loop
                        )
                        try:
                            exited = future.result(timeout=30)
                            logger.info("[TP] safe_exit completed for %s: success=%s",
                                        symbol, exited)
                        except Exception as tp_exit_err:
                            logger.error("[TP] safe_exit failed/timed out for %s: %s",
                                         symbol, tp_exit_err)
                        # An exit that did not happen must not end the watch. safe_exit
                        # now declines to act on a position it cannot verify, and
                        # stopping here would strand it with only its broker stop until
                        # EOD. Back off and let the next pass try again.
                        if not self._retire_or_retry(symbol, exited, "TP_HIT"):
                            continue
                    else:
                        self.stop_focus("TP_HIT")
                    return

                # Stage 2: the midpoint partial (SCALE only)
                if (not manual_override and _tp_mode == 'SCALE'
                        and not t.get('tp_1_hit') and target_reached(ltp, t.get('tp_1'), _tp_dir)
                        and self.order_manager and self._event_loop):
                    exit_qty = t['remaining_qty'] // 2
                    if exit_qty <= 0:
                        t['tp_1_hit'] = True
                        logger.info("[TP-1] %s has 1 share left — no partial to take", symbol)
                    else:
                        logger.info(
                            "🎯 [TP-1 HIT] %s hit midpoint ₹%.2f — scaling out %s of %s",
                            symbol, t['tp_1'], exit_qty, t['remaining_qty'],
                        )
                        # Latched before dispatch so a slow broker cannot let the next
                        # 5Hz tick fire a second partial against the same fill.
                        t['tp_1_hit'] = True

                        # BLOCKING, and the result decides everything below. This was
                        # fire-and-forget until 2026-09-02, when a 429 killed the
                        # partial on NSE:IFCI-EQ while the engine went on believing
                        # 15 of 29 shares had been sold.
                        partial_filled = False
                        try:
                            partial_filled = asyncio.run_coroutine_threadsafe(
                                self.order_manager.partial_exit(symbol, exit_qty, "TP_1_HIT"),
                                self._event_loop,
                            ).result(timeout=25)
                        except Exception as pe:
                            logger.error("[TP-1] %s partial_exit raised: %s", symbol, pe)

                        if not partial_filled:
                            # Nothing sold. Position and stop still agree, so the trade
                            # is safe exactly as it stands — unlatch and let a later
                            # tick retry while price is still beyond the midpoint.
                            t['tp_1_hit'] = False
                            logger.warning(
                                "[TP-1] %s partial did not fill — position unchanged, "
                                "stop still matches. Will retry on a later tick.", symbol,
                            )
                        else:
                            t['remaining_qty'] -= exit_qty
                            if not self._resize_stop_after_partial(symbol, t):
                                return

                # Soft stop. Kept as the non-partial-exit fallback path.
                partial_enabled = False
                if not manual_override and not partial_enabled and self.discretionary_engine and self.order_manager:
                    soft_sl = t['soft_sl']
                    # Direction-aware soft stop.
                    # _trade_dir used to be defined by the breakeven block above;
                    # that block is gone, so it is resolved here where it is used.
                    _trade_dir = t.get('direction', 'SHORT')
                    _soft_hit = (ltp <= soft_sl) if _trade_dir == 'LONG' else (ltp >= soft_sl)
                    if _soft_hit:
                        decision = self.discretionary_engine.evaluate_soft_stop(symbol, t)
                        if decision == 'EXIT':
                            if self.order_manager and self._event_loop:
                                result = False
                                future = asyncio.run_coroutine_threadsafe(
                                    self.order_manager.safe_exit(symbol, "SOFT_STOP"),
                                    self._event_loop
                                )
                                try:
                                    result = future.result(timeout=30)
                                    logger.info(f"[SOFT_STOP] safe_exit completed for {symbol}: success={result}")
                                except Exception as ss_err:
                                    logger.error(f"[SOFT_STOP] safe_exit failed for {symbol}: {ss_err}")
                                if not self._retire_or_retry(symbol, result, "SOFT_STOP"):
                                    continue
                            else:
                                self.stop_focus("SOFT_STOP")
                            return

                                # 5 Hz heartbeat.
                time.sleep(0.2)

                
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


    def stop_focus(self, reason="STOPPED"):
        trade = self.active_trade
        symbol = trade['symbol'] if trade else None
        self.is_running = False
        self.active_trade = None
        if symbol:
            self._exit_retry_at.pop(symbol, None)
            self._exit_retry_count.pop(symbol, None)
        logger.info(f"[FOCUS] Stop. Reason: {reason}")
        
        # Cancel ALL pending orders on any stop
        # Prevents phantom SL order creating accidental LONG after manual close
        if symbol and getattr(config, 'P52_CLEANUP_ON_STOP_FOCUS', True):
            try:
                self.cleanup_orders(symbol)
            except Exception as e:
                logger.error(f"[FOCUS] cleanup_orders failed on stop_focus: {e}")


    def send_sfp_alert(self, trade, ltp):
        if not self.telegram_bot: return
        
        symbol = trade['symbol']
        entry = trade['entry']
        
        msg = (
            f"⚠️ **FAKE OUT DETECTED! (SFP)**\n\n"
            f"[SFP] **{symbol}** trapped buyers!\n"
            f"Price is back below Entry.\n\n"
            f"LTP: *{ltp}*\n"
            f"Key Level: *{entry}*\n\n"
            f"[ACTION] **RE-ENTER SHORT NOW**"
        )
        
        # Send using thread-safe wrapper
        asyncio.create_task(self.telegram_bot.send_alert(msg))
