import time
import logging
import threading
import datetime
import asyncio
import pytz

from fyers_connect import FyersConnect
import config
from order_manager import OrderManager
from discretionary_engine import DiscretionaryEngine
from gate_result_logger import get_gate_result_logger

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
        self.telegram_bot = None # Injected by main.py
        
        # Validation Gate & Cooldown Queue (Phase 37 / 43.4)
        self.pending_signals = {} # {symbol: {signal_data, entry_trigger, invalidation_trigger, timestamp}}
        self.cooldown_signals = {} # Phase 43.4: {symbol: {data, unlock_at}}
        self.monitoring_active = False
        self.monitor_thread = None
        
        # Auto-Recovery on Init
        self.attempt_recovery()
        
        # Phase 52: Event loop reference for sync thread async dispatch
        self._event_loop = None
        
    async def get_position_snapshot(self, symbol: str) -> dict:
        """
        Phase 42.3.1: Provide live position data for Telegram Dashboard.
        Called by telegram_bot.py every 2 seconds.
        """
        if not self.active_trade or self.active_trade.get('symbol') != symbol:
            return None
            
        t = self.active_trade
        return {
            'symbol': t['symbol'],
            'side': t.get('direction', config.TRADE_DIRECTION),  # Phase 94: Use actual direction
            'entry_price': t['entry'],
            'quantity': t['qty'],
            'current_price': t.get('last_price', t['entry']),
            'unrealised_pnl': (t['entry'] - t.get('last_price', t['entry'])) * t['qty'] if t.get('direction', 'SHORT') == 'SHORT' else (t.get('last_price', t['entry']) - t['entry']) * t['qty'],
            'stop_loss': t.get('sl', 0),
            'target': t.get('current_target', 0),
            'sl_state': 'TRAILING' if t.get('target_extended') else 'INITIAL',
            'order_id': t.get('trade_id'),
            'status': t.get('status', 'OPEN'),
            'orderflow_bias': t.get('orderflow_bias', 'NEUTRAL')
        }

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

        # Define Triggers (Phase 51: G12 Tighter Invalidation)
        # Short Logic: Trigger if Price < Low
        entry_trigger = signal_low
        # G12: Tighter invalidation buffer (signal_high * 1.002)
        signal_high = signal_data.get('signal_high', signal_low * 1.01)
        invalidation_trigger = signal_high * config.P51_G12_INVALIDATION_BUFFER_PCT if config.PHASE_51_ENABLED else signal_high * 1.002
        # Actually P51_G12_INVALIDATION_BUFFER_PCT is 0.002. So 1 + 0.002 = 1.002.
        invalidation_trigger = signal_high * (1 + config.P51_G12_INVALIDATION_BUFFER_PCT)

        # Phase 63: Simplified G11 Fixed Timeout (15 minutes)
        IST = pytz.timezone('Asia/Kolkata')
        now_ist = datetime.datetime.now(IST)
        expires_at = now_ist + datetime.timedelta(minutes=15)

        self.pending_signals[symbol] = {
            'data': signal_data,
            'trigger': entry_trigger,
            'invalidate': invalidation_trigger,
            'timestamp': time.time(),
            'expires_at': expires_at, # Phase 51 dynamic timeout
            'queued_at': datetime.datetime.now(),  # FIX #5: for stale signal flush at 9:45
            'correlation_id': signal_data.get('correlation_id'),
            'last_evaluated_minute': None, # Phase 58: for candle-close validation
        }
        
        # Phase 51 [G8.3]: Trigger immediate cooldown for this symbol in SignalManager
        # This prevents other scanner instances (if parallel) from picking it up
        if hasattr(self, 'analyzer') and self.analyzer:
            try:
                self.analyzer.signal_manager.add_pending_signal(symbol)
            except Exception as e:
                logger.error(f"[GATE] Failed to set G8.3 cooldown for {symbol}: {e}")

        logger.info(f"[GATE] Added {symbol} to Validation Gate. Trigger: < {entry_trigger}")
        
        # Start Background Monitor if not running
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
        # Start Background Monitor if not running
        if not self.monitoring_active:
            self.start_pending_monitor()

    def flush_pending_signals(self):
        """Phase 43.4: Promotes signals whose cooldown has expired."""
        now = datetime.datetime.now()
        
        # EOD Guard
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
                    # Assuming SC uses DAY_GAIN_PCT_THRESHOLD as positive scalar e.g. 5.0
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
        # BUG R2 FIX: explicitly pass loop to fallback thread to fix Python 3.12 RuntimeError
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

            # ✅ EOD GUARD — kill the loop at 15:10
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
        Phase 37: Monitors pending signals for Validation Trigger.
        FIX #1: Now async. FIX #3: Slot guard + burn-after-confirm.
        """
        if not self.pending_signals:
            return

        # ✅ EOD GUARD — never execute after 15:10
        now = datetime.datetime.now()
        if now.hour == 15 and now.minute >= 10:
            logger.info(f"[GATE] EOD guard triggered in check_pending_signals. "
                        f"Clearing {len(self.pending_signals)} pending signals.")
            self.stop("EOD_CHECK_GUARD")
            return

        # Create copy to avoid runtime error during modification
        current_pending = list(self.pending_signals.items())
        
        for symbol, pending in current_pending:
            try:
                trigger_price = pending['trigger']
                inval_price = pending['invalidate']
                
                # ── PHASE 58: G12 CANDLE-CLOSE VALIDATION ───────────────────
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
                
                # ── PHASE 51: G10-G12 HARDEING ──────────────────────────────
                # A. CHECK TRIGGER (VALIDATION CONFIRMED)
                # For Short: LTP < Trigger (Signal Low)
                if ltp < trigger_price:
                    # ── G10.1: Execution Precision (Spread Guard) ───────
                    # Soft Gate: Downgrades to CAUTIOUS mode if spread is wide.
                    spread_pct = 0.0
                    try:
                        depth_resp = await asyncio.to_thread(self.fyers.depth, data={"symbol": symbol})
                        if 'd' in depth_resp and symbol in depth_resp['d']:
                            depth = depth_resp['d'][symbol]
                            ask = depth['ask'][0]['price'] if depth['ask'] else ltp
                            bid = depth['bid'][0]['price'] if depth['bid'] else ltp
                            spread_pct = (ask - bid) / ltp if ltp > 0 else 0
                            
                            if spread_pct > config.P51_G10_MAX_SPREAD_PCT:
                                logger.warning(f"⚠️ [WIDE SPREAD] {symbol} spread {spread_pct:.4f} > {config.P51_G10_MAX_SPREAD_PCT} | Downgraded to CAUTIOUS")
                                pending['data']['execution_mode'] = 'CAUTIOUS'
                            else:
                                pending['data']['execution_mode'] = pending['data'].get('execution_mode', 'NORMAL')
                    except Exception as e:
                        logger.warning(f"G10 Spread check failed (non-fatal) for {symbol}: {e}")

                    # G10.2: Entry Price = signal_low - 1 tick
                    tick_size = pending['data'].get('tick_size', 0.05)
                    adjusted_entry = trigger_price - tick_size
                    pending['data']['adjusted_entry'] = adjusted_entry

                    # ── G10-G12 Recording ──────────────────────────────
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

                    # ── AUTO MODE GATE ───────────────────────────────────────────
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

                    # ────────────────────────────────────────────────────────────────
                    # CAPITAL SLOT CHECK (Phase 44.6)
                    # NEW BEHAVIOUR: Signal is fully observed even when capital is locked.
                    # Show Telegram alert just like a real entry — but with "NOT TAKEN" footer.
                    # Log to gate_result_logger as OBSERVED_NO_CAPITAL for ML training.
                    # ────────────────────────────────────────────────────────────────
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

                    # ── EXECUTION COOLDOWN CHECK (Phase 44.6) ───────────────────────
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

                    # ── SLOT GUARD (Signal Manager) ──────────────────────────────────
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

                    # ── ORDER MANAGER GUARD ──────────────────────────────────────────
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

                    # ── EXEC COOLDOWN GATE (Phase 44.6) ─────────────────────────────
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
                    # ────────────────────────────────────────────────────────────────

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
                        logger.warning(f"⚠️ [EXECUTION FAILED] {symbol} — enter_position returned: {pos}")
                        if self.telegram_bot:
                            asyncio.create_task(self.telegram_bot.send_alert(
                                f"⚠️ ORDER FAILED: {symbol} — broker returned {pos}\n"
                                f"⏳ 15-min execution cooldown set."
                            ))

                    del self.pending_signals[symbol]
                    continue
                    
                # B. CHECK INVALIDATION / TIMEOUT
                elif ltp > inval_price:
                    logger.info(f"🚫 [INVALIDATED] {symbol} hit G12 tighter buffer {inval_price}")
                    _queue_validation_update(outcome='REJECTED', details={'reason': 'G12_INVALIDATED_BUFFER', 'ltp': ltp})
                    del self.pending_signals[symbol]
                    continue
                
                # C. TIMEOUT (Phase 51 G11: Dynamic expires_at)
                elif datetime.datetime.now(pytz.timezone('Asia/Kolkata')) > pending.get('expires_at', datetime.datetime.now(pytz.timezone('Asia/Kolkata')) + datetime.timedelta(minutes=15)):
                    logger.info(f"⌛ [TIMEOUT] {symbol} expired at {pending.get('expires_at')}")
                    _queue_validation_update(outcome='TIMEOUT', details={'reason': 'G11_DYNAMIC_TIMEOUT'})
                    del self.pending_signals[symbol]
                    continue
                
                # No further action needed if within range
                else:
                    pass
                     
            except Exception as e:
                logger.error(f"Validation Check Error {symbol}: {e}")
                # FIX #3: Alert on execution error instead of silent swallow
                if self.telegram_bot:
                    asyncio.create_task(self.telegram_bot.send_alert(
                        f"🔴 EXECUTION ERROR {symbol}: {e}"
                    ))
        
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
        Latch onto a trade. Phase 94: Direction-aware.
        """
        # Adapt to OrderManager state or Legacy
        entry_price = position_data.get('entry_price', position_data.get('entry', 0))
        sl_price = position_data.get('stop_loss',
                       position_data.get('hard_stop_price',
                           position_data.get('sl', 0)))
        actual_qty = position_data.get('qty', qty)

        # Phase 94: Read direction from config
        direction = config.TRADE_DIRECTION  # 'SHORT' or 'LONG'
        is_long = direction == 'LONG'

        # Soft SL: opposite side from entry
        if is_long:
            soft_sl = entry_price * (1 - config.DISCRETIONARY_CONFIG['soft_stop_pct'])
        else:
            soft_sl = entry_price * (1 + config.DISCRETIONARY_CONFIG['soft_stop_pct'])

        # Phase 52: Compute TP levels from OrderManager
        tps = {}
        if (self.order_manager and entry_price > 0):
            try:
                tps = self.order_manager.compute_take_profits(entry_price, position_data)
            except Exception as e:
                logger.warning(f"[P52] compute_take_profits failed for {symbol}: {e}")

        # TP default: 1.5% in the correct direction
        tp_default = entry_price * 1.015 if is_long else entry_price * 0.985

        self.active_trade = {
            'symbol':          symbol,
            'entry':           entry_price,
            'sl':              sl_price,
            'soft_sl':         soft_sl,
            'tp':              tps.get('tp', tp_default),
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
            'direction':       direction,  # Phase 94: Store direction for TP/BE/PnL logic
            
            # Phase 89.9: Precalculated True Breakeven (3.5% profit @ 5x)
            # SHORT: Trigger = 0.7% drop, BE SL = 0.25% drop
            # LONG:  Trigger = 0.7% rise, BE SL = 0.25% rise
            'be_trigger':      entry_price * 1.007 if is_long else entry_price * 0.993,
            'be_sl':           entry_price * 1.0025 if is_long else entry_price * 0.9975,
            'be_activated':    False,

            # Phase 96: MFE/MAE tracking for ML trainer
            'mfe_pct':         0.0,  # Max Favorable Excursion (% from entry)
            'mae_pct':         0.0,  # Max Adverse Excursion (% from entry)
        }
        
        self.is_running = True
        logger.info(
            f"[FOCUS] Started {symbol} qty={actual_qty} entry=₹{entry_price:.2f} "
            f"tp=₹{self.active_trade['tp']:.2f} sl=₹{sl_price:.2f}"
        )
        
        # Start Loop
        self.thread = threading.Thread(target=self.focus_loop, daemon=True)
        self.thread.start()

    def _check_broker_position(self, symbol: str) -> dict:
        """
        Phase 42/95: Query broker for current position.
        Returns:
          - Position dict if found
          - None if API succeeded but position not found (genuinely flat)
          - {'_api_failed': True} if API call failed (DO NOT treat as flat)
        """
        try:
            positions = self.fyers.positions()
            if positions.get('s') != 'ok' or 'netPositions' not in positions:
                logger.error("[SAFETY] Could not fetch positions (API error, not flat)")
                return {'_api_failed': True}

            for pos in positions.get('netPositions', []):
                if pos['symbol'] == symbol:
                    return pos

            return None  # API succeeded, position genuinely not found = closed

        except Exception as e:
            logger.error(f"[SAFETY] Broker position check failed: {e}")
            return {'_api_failed': True}

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
                    
                    # Phase 52: monitor_hard_stop_status is async — dispatch correctly from sync thread
                    if self._event_loop:
                        asyncio.run_coroutine_threadsafe(
                            self.order_manager.monitor_hard_stop_status(symbol),
                            self._event_loop
                        )

                # ── CRITICAL: EOD SQUARE-OFF (15:10) ────────────────
                now = datetime.datetime.now()
                if now.hour == 15 and now.minute >= 10:
                    logger.warning(f"⏰ [EOD] Force Closing {symbol} at 15:10")
                    if self.order_manager and self._event_loop:
                        asyncio.run_coroutine_threadsafe(
                            self.order_manager.safe_exit(symbol, "EOD_SQUARE_OFF"),
                            self._event_loop
                        )
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

                # Skip cycle if no price available
                if not ltp:
                    time.sleep(1)
                    continue

                self.active_trade['last_price'] = ltp
                t = self.active_trade

                # ── Phase 96: Track MFE/MAE on every tick ──────────────────
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

                # ── Phase 95: MANUAL CLOSE DETECTION (Broker-side) ──────────
                # Every ~5 seconds, check if the broker still has this position.
                # If not, the user closed it manually via the app.
                # SAFETY: Require 2 consecutive CONFIRMED flat reads to avoid
                # false positives from transient API failures.
                # Phase 97: Exponential backoff on API failures to avoid rate-limit storms.
                _last_broker_check = getattr(self, '_last_broker_pos_check', 0)
                _api_fail_streak = getattr(self, '_api_fail_streak', 0)
                _check_interval = min(5 * (2 ** _api_fail_streak), 30)  # 5s → 10s → 20s → 30s max
                if time.time() - _last_broker_check > _check_interval:
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
                            logger.info(
                                f"💰 [MANUAL EXIT] {symbol} {_dir} | Entry ₹{entry_p:.2f} → Exit ~₹{exit_p:.2f} | "
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
                                            reason='MANUAL_CLOSE_DETECTED',
                                            exit_price=exit_p,
                                            pnl=pnl,
                                            send_alert=True
                                        ),
                                        self._event_loop
                                    )
                                    future.result(timeout=10)
                                except Exception as e:
                                    logger.error(f"[FOCUS] _finalize_closed_position failed: {e}")
                                    # Fallback: release capital directly
                                    if self.order_manager.capital:
                                        asyncio.run_coroutine_threadsafe(
                                            self.order_manager.capital.release_slot(broker=self.order_manager.broker),
                                            self._event_loop
                                        )

                            if self.telegram_bot and self._event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    self.telegram_bot.send_alert(
                                        f"👻 **MANUAL CLOSE DETECTED**\n\n"
                                        f"Symbol: `{symbol}`\n"
                                        f"Entry: ₹{entry_p:.2f}\n"
                                        f"Exit: ~₹{exit_p:.2f}\n"
                                        f"PnL: ₹{pnl:.2f}\n\n"
                                        f"✅ Capital slot released.\n"
                                        f"✅ Bot state synced."
                                    ),
                                    self._event_loop
                                )

                            self.stop_focus("MANUAL_CLOSE")
                            return

                    else:
                        # Position exists on broker — reset flat counter & backoff
                        self._consecutive_flat_reads = 0
                        self._api_fail_streak = 0


                # ── Phase 89.9: TRUE BREAKEVEN (3.5% Trigger) ──────────────
                # Phase 94: Direction-aware comparison
                _trade_dir = t.get('direction', 'SHORT')
                _be_hit = (ltp >= t['be_trigger']) if _trade_dir == 'LONG' else (ltp <= t['be_trigger'])
                if not t.get('be_activated', False) and _be_hit:
                    new_sl = t['be_sl']
                    old_sl = t['sl']
                    t['sl'] = new_sl
                    t['be_activated'] = True
                    
                    logger.info(f"🛡️ [PROTECTION] {symbol} up 3.5% (leveraged)! SL → ₹{new_sl:.2f} (Fee-Protected BE)")

                    # Phase 97.2: Move the ACTUAL broker SL-M order to the BE price
                    if self.order_manager and self._event_loop:
                        asyncio.run_coroutine_threadsafe(
                            self.order_manager.move_hard_stop(symbol, new_sl),
                            self._event_loop
                        )
                    
                    if self.telegram_bot and self._event_loop:
                        msg = (
                            f"🛡️ **Fee-Protected BE Activated**\n"
                            f"Symbol: `{symbol}`\n"
                            f"Target: +3.5% hit (0.7% move)\n"
                            f"New SL: ₹{new_sl:.2f} (+0.25% profit zone)\n"
                            f"_Your fees are now covered._"
                        )
                        asyncio.run_coroutine_threadsafe(
                            self.telegram_bot.send_alert(msg),
                            self._event_loop
                        )
                
                # ── PHASE 78: SINGLE TP ENGINE ────────────────────────────
                # Phase 94: Direction-aware TP comparison
                _tp_hit = (ltp >= t['tp']) if _trade_dir == 'LONG' else (ltp <= t['tp'])
                if _tp_hit:
                    logger.info(f"🎯 [TP] {symbol} hit ₹{t['tp']:.2f} — closing 100% ({t['remaining_qty']} shares)")
                    if self.order_manager and self._event_loop:
                        asyncio.run_coroutine_threadsafe(
                            self.order_manager.safe_exit(symbol, "TP_HIT"),
                            self._event_loop
                        )
                    self.stop_focus("TP_HIT")
                    return

                # ── SOFT STOP (existing logic — keep for non-partial-exit fallback) ──
                partial_enabled = False  # Phase 93: Partial exit not currently active
                if not partial_enabled and self.discretionary_engine and self.order_manager:
                    soft_sl = t['soft_sl']
                    # Phase 94: Direction-aware soft stop
                    _soft_hit = (ltp <= soft_sl) if _trade_dir == 'LONG' else (ltp >= soft_sl)
                    if _soft_hit:
                        decision = self.discretionary_engine.evaluate_soft_stop(symbol, t)
                        if decision == 'EXIT':
                            if self._event_loop:
                                asyncio.run_coroutine_threadsafe(
                                    self.order_manager.safe_exit(symbol, "SOFT_STOP"),
                                    self._event_loop
                                )
                            return

                # ── FALLBACK / LEGACY LOGIC ──────────────────────
                # Keep simplistic trailing if Discretionary Engine not active?
                # Or just rely on Hard SL (monitored by order_manager)
                    
                    
                # Phase 89.9: High-frequency heartbeat for 200ms latency execution
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

    def stop_focus(self, reason="STOPPED"):
        trade = self.active_trade
        symbol = trade['symbol'] if trade else None
        self.is_running = False
        self.active_trade = None
        logger.info(f"[FOCUS] Stop. Reason: {reason}")
        
        # Phase 52: Cancel ALL pending orders on any stop
        # Prevents phantom SL order creating accidental LONG after manual close
        if symbol and getattr(config, 'P52_CLEANUP_ON_STOP_FOCUS', True):
            try:
                self.cleanup_orders(symbol)
            except Exception as e:
                logger.error(f"[FOCUS] cleanup_orders failed on stop_focus: {e}")

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

