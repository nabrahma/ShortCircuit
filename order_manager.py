"""
Phase 44.6: Async Order Manager
Changes from Phase 44.4:
  1. compute_qty() via CapitalManager.compute_qty() — full Fyers margin utilization
  2. acquire_slot() called after confirmed fill (was NEVER called before → capital never consumed)
  3. Fill timeout reduced 30s → 15s with REST verification fallback
  4. Execution failure cooldown: 15-min block per symbol after any failed entry
  5. _finalize_closed_position() calls async release_slot(broker) (not deprecated release())
"""

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, timedelta, UTC
from typing import Dict, Optional, Any
import config
from fyers_broker_interface import FyersBrokerInterface
from ml_logger import get_ml_logger


logger = logging.getLogger(__name__)

FYERS_ORDER_STATUS_TRADED  = 2
FYERS_ORDER_STATUS_PENDING = 6
EXEC_COOLDOWN_SECONDS      = 900   # 15 minutes after any failed entry


class OrderManager:
    """
    Phase 44.6: Async Order Manager with WebSocket Support.

    Responsibilities:
    1. Async Execution via FyersBrokerInterface
    2. Zero-latency Fill Detection (WebSocket, 15s timeout with REST fallback)
    3. Full Fyers margin utilization via CapitalManager.compute_qty()
    4. Capital slot acquisition after fill / release after close
    5. Execution failure cooldown (prevents same-symbol spam)
    6. Safe Entry with Immediate Hard Stop (SL-M)
    7. Phantom Fill Prevention (Cancel SL *before* Exit)
    """

    def __init__(
        self,
        broker: FyersBrokerInterface,
        telegram_bot,
        db=None,
        capital_manager=None,
        trade_manager=None
    ):
        self.broker  = broker
        self.telegram = telegram_bot
        self.db      = db
        self.capital = capital_manager
        self.trade_manager = trade_manager

        self.active_positions: Dict[str, Any] = {}
        self.position_locks:   Dict[str, asyncio.Lock] = {}
        self.exit_in_progress: Dict[str, bool] = {}
        self.hard_stops:       Dict[str, str]  = {}
        self.partial_exits_in_progress: Dict[str, Dict[str, float]] = {} # Phase 77: {symbol: {reason: timestamp}}

        # FIX 4: Execution failure cooldown tracker
        # { symbol: datetime_unblock }
        self._exec_cooldowns: Dict[str, datetime] = {}

    # ─────────────────────────────────────────────────────────────────────────
    # Helpers
    # ─────────────────────────────────────────────────────────────────────────

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self.position_locks:
            self.position_locks[symbol] = asyncio.Lock()
        return self.position_locks[symbol]

    def is_exec_cooldown_active(self, symbol: str) -> tuple:
        """
        Returns (is_active: bool, remaining_seconds: int).
        Used by focus_engine before calling enter_position.
        """
        if symbol not in self._exec_cooldowns:
            return False, 0
        unblock_at = self._exec_cooldowns[symbol]
        now = datetime.now(UTC)
        if now < unblock_at:
            remaining = int((unblock_at - now).total_seconds())
            return True, remaining
        # Cooldown expired — clean up
        del self._exec_cooldowns[symbol]
        return False, 0

    def _set_exec_cooldown(self, symbol: str, reason: str, seconds: int = 900):
        """Phase 44.6: Block symbol from new entries after local logic failure."""
        unblock_at = datetime.now(UTC) + timedelta(seconds=seconds)
        self._exec_cooldowns[symbol] = unblock_at
        logger.warning(
            f"⏳ [COOLDOWN] {symbol} blocked for {seconds}s | Reason: {reason} | Until: {unblock_at.strftime('%H:%M:%S')}"
        )

    @staticmethod
    def _round_sl_to_tick(price: float, side: str, tick: float = 0.05) -> float:
        """
        Round SL trigger price to nearest valid Fyers tick boundary.

        Fyers rejects SL-M orders where trigger_price % tick_size != 0.
        NSE equities: tick_size varies per stock (0.01, 0.05, 0.10).
        Always use the tick_size from the symbol master, never hardcode.

        Rounding direction (away from entry = more buffer, never tighter):
          SHORT (SELL entry) → SL is above entry → round UP (ceiling)
          LONG  (BUY entry)  → SL is below entry → round DOWN (floor)

        Examples:
          SHORT: 745.16 → ceil(745.16/0.05)*0.05 = ceil(14903.2)*0.05 = 745.20  ✅
          SHORT: 745.20 → 745.20  (already valid, no change)
          LONG:  718.94 → floor(718.94/0.05)*0.05 = floor(14378.8)*0.05 = 718.90  ✅
        """
        import math
        if side == 'SELL':   # SHORT trade — SL is above entry
            rounded = math.ceil(price / tick) * tick
        else:                # LONG trade — SL is below entry
            rounded = math.floor(price / tick) * tick
        return round(rounded, 2)

    def compute_stop_loss(self, ltp: float, signal: dict) -> float:
        """Phase 51: ATR-based SL calculation. Phase 94: Direction-aware."""
        atr    = signal.get('atr', 0)
        tick   = signal.get('tick_size', 0.05)
        # PRD: max(atr * 0.5, 3 * tick_size) — using config constants
        buffer = max(atr * getattr(config, 'P51_SL_ATR_MULTIPLIER', 0.5),
                     tick * getattr(config, 'P51_SL_MIN_TICK_BUFFER', 3))
        
        direction = config.TRADE_DIRECTION
        if direction == 'LONG':
            # For LONG trades, SL is below entry/low
            signal_low = signal.get('signal_low', ltp * 0.99)
            sl_price = signal_low - buffer
            return self._round_sl_to_tick(sl_price, 'BUY', tick)
        else:
            # For SHORT trades, SL is above entry/high
            signal_high = signal.get('signal_high', ltp * 1.01)
            sl_price = signal_high + buffer
            return self._round_sl_to_tick(sl_price, 'SELL', tick)

    def compute_take_profits(self, entry: float, signal: dict) -> dict:
        """Phase 78: Single 100% Take Profit Target. Phase 94: Direction-aware."""
        atr = signal.get('atr', 0)
        direction = config.TRADE_DIRECTION
        if atr == 0:
            # Default 1.5% TP in the correct direction
            return {'tp': entry * 1.015 if direction == 'LONG' else entry * 0.985}

        # Use signal override or default
        tp_mult = signal.get('tp_atr_mult_override') or \
                  signal.get('tp1_atr_mult_override') or \
                  getattr(config, 'P78_SINGLE_TP_ATR_MULT_DEFAULT', 1.0)

        if direction == 'LONG':
            tp = entry + atr * tp_mult
        else:
            tp = entry - atr * tp_mult
        return {'tp': tp}

    async def _verify_fill_via_rest(self, order_id: str) -> Optional[float]:
        """
        FIX 3: REST fallback when fill timeout fires but cancel returns
        'not a pending order' — means fill arrived but WS event was dropped.
        Returns fill price if confirmed filled, None otherwise.
        """
        try:
            loop = asyncio.get_event_loop()
            rest = getattr(self.broker, 'rest_client', None)
            if not rest:
                return None
            orderbook = await loop.run_in_executor(None, rest.orderbook)
            if not isinstance(orderbook, dict) or orderbook.get('s') != 'ok':
                return None
            for order in orderbook.get('orderBook', []):
                if str(order.get('id')) == str(order_id):
                    if order.get('status') == FYERS_ORDER_STATUS_TRADED:
                        for key in ('tradedPrice', 'tradePrice', 'limitPrice'):
                            val = order.get(key, 0)
                            if val:
                                logger.warning(
                                    f"🔍 REST VERIFY: order {order_id} IS FILLED "
                                    f"(WS drop detected) fill_price=₹{val}"
                                )
                                return float(val)
            return None
        except Exception as e:
            logger.error(f"REST fill verify failed for {order_id}: {e}")
            return None

    # ─────────────────────────────────────────────────────────────────────────
    # Close Path
    # ─────────────────────────────────────────────────────────────────────────

    async def _finalize_closed_position(
        self,
        symbol: str,
        reason: str,
        exit_price: float = 0.0,
        pnl: float = 0.0,
        send_alert: bool = False,
    ) -> None:
        """Shared close-path. Cleans state, releases capital, logs DB."""
        
        pos = self.active_positions.get(symbol)

        # Phase 71: Update ML Outcome
        if pos and pos.get('obs_id'):
            try:
                # Determine outcome label
                outcome = "BREAKEVEN"
                if pnl > 0: 
                    outcome = "WIN"
                elif pnl < 0: 
                    outcome = "LOSS"
                
                # Calculate hold time (mins)
                hold_time = 0
                if pos.get('entry_time'):
                    elapsed = (datetime.now() - pos['entry_time']).total_seconds()
                    hold_time = int(elapsed / 60)

                # Calculate real pnl_pct based on dollar PNL (works for LONG/SHORT)
                pnl_pct = 0.0
                entry_price = pos.get('entry_price', 0)
                qty = pos.get('qty', 1)
                if entry_price > 0 and qty > 0:
                    pnl_pct = (pnl / (entry_price * qty)) * 100

                # ML Update — Phase 96: Include MFE/MAE from focus_engine
                get_ml_logger().update_outcome(
                    obs_id=pos['obs_id'],
                    outcome=outcome,
                    exit_price=exit_price,
                    max_favorable=pos.get('mfe_pct', 0),
                    max_adverse=pos.get('mae_pct', 0),
                    hold_time_mins=hold_time,
                    pnl_pct=pnl_pct
                )
                logger.info(f"   [ML] Outcome recorded for {symbol} (obs={pos['obs_id']}) "
                            f"MFE={pos.get('mfe_pct', 0):.2f}% MAE={pos.get('mae_pct', 0):.2f}% PNL={pnl_pct:.2f}%")
                
                # Phase 72: Jarvis Broadcast
                from dashboard_bridge import get_dashboard_bridge
                get_dashboard_bridge().broadcast("ORDER_EVENT", {
                    "symbol": symbol,
                    "type": "EXIT",
                    "pnl": pnl,
                    "reason": reason
                })
            except Exception as e:
                logger.error(f"❌ [ML-OUTCOME] Failed for {symbol}: {e}")

        # FIX 5: use async release_slot (re-syncs Fyers margin after close)
        if self.capital:
            try:
                await self.capital.release_slot(broker=self.broker)
            except Exception as e:
                logger.error(f"[CLOSE] Capital release_slot failed for {symbol}: {e}")

        if self.db:
            try:
                await self.db.log_trade_exit(
                    symbol,
                    {
                        'exit_price': exit_price,
                        'pnl': pnl,
                        'exit_reason': reason,
                        'status': 'CLOSED',
                    }
                )
            except Exception as e:
                logger.error(f"[CLOSE] DB close log failed for {symbol}: {e}")

        # Phase 51 [G13]: Record outcome in SignalManager for loss tracking
        try:
            pos = self.active_positions.get(symbol)
            if pos:
                if self.trade_manager:
                    self.trade_manager.record_trade_outcome(symbol, pnl)
                else:
                    # Fallback to direct call if trade_manager not injected
                    from signal_manager import get_signal_manager
                    get_signal_manager().record_outcome(symbol, pnl)
                    logger.info(f"Phase 69 Outcome recorded for {symbol} (direct): ₹{pnl:.2f}")
        except Exception as e:
            logger.error(f"[CLOSE] G13 record failed: {e}")

        # Final state cleanup
        self.active_positions.pop(symbol, None)
        self.hard_stops.pop(symbol, None)
        self.exit_in_progress.pop(symbol, None)

        if send_alert and self.telegram and hasattr(self.telegram, 'send_alert'):
            try:
                await self.telegram.send_alert(
                    f"🛑 **HARD STOP FILLED**\n\n"
                    f"Symbol: `{symbol}`\n"
                    f"Exit Price: ₹{exit_price:.2f}\n"
                    f"Reason: {reason}"
                )
            except Exception:
                pass

    # ─────────────────────────────────────────────────────────────────────────
    # Hard Stop Monitor
    # ─────────────────────────────────────────────────────────────────────────

    async def monitor_hard_stop_status(self, symbol: str) -> bool:
        """
        Detects SL fill from broker orderbook.
        Returns True when hard-stop fill detected and state is closed.
        (Unchanged from Phase 44.4)
        """
        lock = self._get_lock(symbol)
        async with lock:
            pos = self.active_positions.get(symbol)
            if not pos or pos.get('status') != 'OPEN':
                return False

            sl_id = pos.get('sl_id') or self.hard_stops.get(symbol)
            if not sl_id:
                return False

            try:
                orderbook = None
                rest_client = getattr(self.broker, 'rest_client', None)
                if rest_client and hasattr(rest_client, 'orderbook'):
                    loop = asyncio.get_event_loop()
                    orderbook = await loop.run_in_executor(None, rest_client.orderbook)
                elif hasattr(self.broker, 'orderbook'):
                    loop = asyncio.get_event_loop()
                    orderbook = await loop.run_in_executor(None, self.broker.orderbook)

                if not isinstance(orderbook, dict) or orderbook.get('s') != 'ok':
                    return False

                for order in orderbook.get('orderBook', []):
                    if str(order.get('id')) != str(sl_id):
                        continue

                    if order.get('status') == FYERS_ORDER_STATUS_TRADED:
                        exit_price = 0.0
                        for price_key in ('tradedPrice', 'tradePrice', 'limitPrice', 'stopPrice'):
                            try:
                                raw = order.get(price_key, 0)
                                if raw:
                                    exit_price = float(raw)
                                    break
                            except Exception:
                                continue

                        logger.warning(
                            f"[HARD_STOP] Filled for {symbol} (sl_id={sl_id}). "
                            "Syncing state/capital/db cleanup."
                        )
                        await self._finalize_closed_position(
                            symbol=symbol,
                            reason='HARD_STOP_FILLED',
                            exit_price=exit_price,
                            pnl=0.0,
                            send_alert=True,
                        )
                        return True
                    return False

            except Exception as e:
                logger.error(f"[HARD_STOP] Monitor failed for {symbol}: {e}")

            return False

    # ─────────────────────────────────────────────────────────────────────────
    # Today's Trades
    # ─────────────────────────────────────────────────────────────────────────

    async def get_today_trades(self) -> list:
        try:
            positions = await self.broker.get_all_positions()
            trades = []
            for p in positions:
                trades.append({
                    'symbol':         p.get('symbol', 'UNKNOWN'),
                    'realised_pnl':   float(p.get('realized_profit', 0)),
                    'unrealised_pnl': float(p.get('unrealized_profit', 0)),
                    'qty':            p.get('netQty', 0)
                })
            return trades
        except Exception as e:
            logger.error(f"Error fetching today's trades: {e}")
            return []

    # ─────────────────────────────────────────────────────────────────────────
    # Startup Reconciliation
    # ─────────────────────────────────────────────────────────────────────────

    async def startup_reconciliation(self):
        """
        Runs at startup to sync state.
        Phase 44.6: Also triggers initial capital sync from Fyers.
        """
        import time
        start_time = time.time()
        logger.info("🔍 [STARTUP] Running Async Order Reconciliation...")

        try:
            # DB Pool Warmup
            if self.db:
                try:
                    pool = await self.db.get_pool()
                    async with pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                    logger.info("DB Pool warmed up.")
                except Exception as e:
                    logger.warning(f"DB Pool warmup failed: {e}")

            # FIX 2 (Startup): Initial capital sync from Fyers
            if self.capital:
                await self.capital.sync(self.broker)

            # Orphan Check
            open_positions = await self.broker.get_all_positions()
            for pos in open_positions:
                qty    = pos.get('qty', 0)
                symbol = pos.get('symbol')
                if qty != 0:
                    logger.critical(f"⚠️ [STARTUP] ORPHAN FOUND: {symbol} Qty: {qty}")
                    if self.telegram:
                        await self.telegram.send_alert(f"⚠️ **ORPHAN**: {symbol} ({qty})")

            # Cancel Pending Orders
            loop = asyncio.get_event_loop()
            orderbook = await loop.run_in_executor(None, self.broker.rest_client.orderbook)
            if orderbook and isinstance(orderbook, dict) and orderbook.get('s') == 'ok':
                pending = [o for o in orderbook.get('orderBook', []) if o['status'] == FYERS_ORDER_STATUS_PENDING]
                for order in pending:
                    logger.info(f"[STARTUP] Cancelling stale order {order['id']}")
                    await self.broker.cancel_order(order['id'])

            elapsed_ms = (time.time() - start_time) * 1000
            if elapsed_ms > 3000:
                logger.error(f"CRITICAL Slow Reconciliation {elapsed_ms:.0f}ms")
                if self.telegram:
                    await self.telegram.send_alert(f"⚠️ Reconciliation lag {elapsed_ms:.0f}ms")
            elif elapsed_ms > 1500:
                logger.error(f"Slow Reconciliation {elapsed_ms:.0f}ms")
            elif elapsed_ms > 500:
                logger.warning(f"Slow Reconciliation {elapsed_ms:.0f}ms")

            logger.info("✅ [STARTUP] Reconciliation Done.")

        except Exception as e:
            logger.critical(f"🔥 [STARTUP] Failed: {e}")

    # ─────────────────────────────────────────────────────────────────────────
    # ENTRY — Core Fix
    # ─────────────────────────────────────────────────────────────────────────

    async def enter_position(self, signal: dict) -> Optional[dict]:
        """
        Phase 44.6: Async Entry + SL-M with full capital utilization.

        FIX 1: compute_qty() uses real Fyers margin, not virtual/hardcoded figure.
        FIX 2: acquire_slot() called after confirmed fill (was missing entirely).
        FIX 3: Fill timeout 30s → 15s with REST verification fallback.
        FIX 4: Execution failure cooldown set on any failed entry.
        """
        symbol = signal['symbol']
        lock   = self._get_lock(symbol)

        async with lock:
            logger.info(f"🚀 [ENTRY] Processing {symbol}...")

            # ── Auto Mode Gate ────────────────────────────────────────────
            if self.telegram and hasattr(self.telegram, 'is_auto_mode'):
                if not self.telegram.is_auto_mode():
                    logger.critical(
                        f"🚫 ORDER BLOCKED: enter_position called while auto_mode=False. "
                        f"Signal: {symbol}. This is a bug — focus_engine should have caught this."
                    )
                    return None

            # ── FIX 1: Sizing via compute_qty (full Fyers margin utilization) ──
            ltp = signal.get('ltp', 0)
            if ltp == 0:
                ltp = await self.broker.get_ltp(symbol) or 0
            if ltp == 0:
                logger.error(f"❌ [ENTRY] {symbol}: LTP is 0, cannot size position")
                self._set_exec_cooldown(symbol, reason='LTP_ZERO', seconds=300)
                return None


            # Phase 91.2: G14 Leverage Guard removed.
            # For intraday (MIS) orders, Fyers assigns leverage automatically.
            # If a stock doesn't qualify, Fyers throws an API error caught below.



            if self.capital:
                qty, required_capital, margin_req = self.capital.compute_qty(symbol, ltp)
            else:
                # Fallback if capital manager not injected
                buying_power = 9000.0
                raw_qty = buying_power / ltp
                qty = int(math.floor(raw_qty))
                required_capital = qty * ltp
                margin_req = required_capital / 5.0
                logger.warning(f"[SIZING] Capital manager not injected — using fallback ₹{buying_power}")

            # PRD: Spread > 0.4% -> CAUTIOUS execution (reduced size)
            if signal.get('execution_mode') == 'CAUTIOUS':
                old_qty = qty
                qty = int(math.floor(qty * 0.5))
                required_capital *= 0.5
                margin_req *= 0.5
                logger.warning(f"⚠️ [CAUTIOUS SIZE] {symbol} qty reduced from {old_qty} to {qty} (50%)")

            # ── Qty Zero Guard ────────────────────────────────────────────
            if qty == 0:
                real_margin = self.capital._real_margin if self.capital else 0
                msg = (
                    f"🚫 *ORDER BLOCKED — QTY ZERO*\n\n"
                    f"Symbol:  `{symbol}`\n"
                    f"LTP:     ₹{ltp:.2f}\n"
                    f"Margin:  ₹{real_margin:.2f}\n"
                    f"BuyPwr:  ₹{real_margin * 5:.2f}\n\n"
                    f"Stock too expensive for available margin.\n"
                    f"Need ≥ ₹{ltp/5:.2f} real margin per share."
                )
                logger.warning(f"❌ [ENTRY] {symbol}: qty=0 at ltp=₹{ltp:.2f}")
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(msg)
                self._set_exec_cooldown(symbol, reason='ZERO_QTY', seconds=300)
                return None

            # Phase 94: Read direction from config runtime switch
            signal_type = config.TRADE_DIRECTION
            side = 'SELL' if signal_type == 'SHORT' else 'BUY'

            logger.info(
                f"[PRE-EXEC] {symbol} {side} qty={qty} @ ₹{ltp:.2f} "
                f"cost=₹{required_capital:.2f} margin_req=₹{margin_req:.2f}"
            )

            try:
                # ── Step 1: Place Entry Order ─────────────────────────────
                entry_id = await self.broker.place_order(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type='MARKET'
                )
                logger.info(f"✅ Entry Placed: {entry_id} | {symbol} {side} ×{qty}")

                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(
                        f"✅ *ENTRY ORDER PLACED*\n\n"
                        f"Symbol: `{symbol}` {side}\n"
                        f"Qty: {qty} × ₹{ltp:.2f}\n"
                        f"Cost: ₹{required_capital:.2f}\n"
                        f"Order ID: `{entry_id}`"
                    )

                # ── FIX 3: Wait for Fill — 15s with REST fallback ─────────
                filled = await self.broker.wait_for_fill(entry_id, timeout=15.0)

                if not filled:
                    logger.warning(f"⚠️ [ENTRY] Fill timeout for {entry_id} — attempting cancel...")
                    cancel_result = await self.broker.cancel_order(entry_id)

                    # FIX 3b: If cancel says "not a pending order",
                    # the fill arrived but WS event was dropped.
                    # Verify via REST before declaring failure.
                    cancel_err = str(cancel_result).lower() if cancel_result else ""
                    if 'not a pending' in cancel_err or '-52' in cancel_err or cancel_result is False:
                        rest_fill_price = await self._verify_fill_via_rest(entry_id)
                        if rest_fill_price:
                            logger.warning(
                                f"[ENTRY] WS drop recovered — order {entry_id} "
                                f"WAS filled @ ₹{rest_fill_price:.2f}. Continuing with fill."
                            )
                            filled = True
                            ltp = rest_fill_price   # use actual fill price
                        else:
                            logger.error(
                                f"❌ [ENTRY] Fill timeout AND REST shows not filled "
                                f"for {symbol} order {entry_id}"
                            )
                    
                    if not filled:
                        # FIX 4: Set 20-min cooldown on genuine fill timeout
                        self._set_exec_cooldown(symbol, reason='FILL_TIMEOUT', seconds=1200)
                        if self.telegram and hasattr(self.telegram, 'send_alert'):
                            await self.telegram.send_alert(
                                f"❌ *ENTRY FILL TIMEOUT*\n\n"
                                f"Symbol: `{symbol}`\n"
                                f"Order ID: `{entry_id}`\n"
                                f"Action: Order cancelled.\n"
                                f"⏳ Cooldown: 20 min"
                            )
                        return None

                # ── FIX 2: Acquire Capital Slot AFTER confirmed fill ───────
                # (This was completely missing before — capital was NEVER consumed)
                if self.capital:
                    await self.capital.acquire_slot(symbol)

                # ── FIX 4: ATR-Based SL ───────────────────────────────────
                stop_price = self.compute_stop_loss(ltp, signal)
                sl_side    = 'BUY' if side == 'SELL' else 'SELL'

                tick = signal.get('tick_size', 0.05)
                logger.info(
                    f"[SL-CALC] {symbol} ATR-based stop_price=₹{stop_price:.2f} (tick={tick})"
                )

                try:
                    sl_id = await self.broker.place_order(
                        symbol=symbol,
                        side=sl_side,
                        qty=qty,
                        order_type='SL_MARKET',
                        trigger_price=stop_price
                    )
                except Exception as sl_exc:
                    sl_id = None
                    sl_error = str(sl_exc)
                    logger.critical(
                        f"🚨 [SL-FAIL] SL placement raised exception for {symbol}: {sl_error}"
                    )
                    if self.telegram and hasattr(self.telegram, 'send_alert'):
                        await self.telegram.send_alert(
                            f"🚨 *SL PLACEMENT FAILED*\n\n"
                            f"Symbol: `{symbol}`\n"
                            f"Entry filled @ ₹{ltp:.2f} — SL order threw exception.\n"
                            f"Error: `{sl_error[:150]}`\n"
                            f"StopPrice attempted: ₹{stop_price:.2f}\n"
                            f"⚡ Emergency exit triggered. Capital slot released."
                        )
                    await self._emergency_exit(symbol, qty, sl_side)
                    if self.capital:
                        await self.capital.release_slot(broker=self.broker)
                    self._set_exec_cooldown(symbol, reason='SL_EXCEPTION', seconds=EXEC_COOLDOWN_SECONDS)
                    return None

                if not sl_id:
                    logger.critical(
                        f"🚨 [SL-FAIL] SL placement returned None for {symbol} "
                        f"(stop_price=₹{stop_price:.2f})"
                    )
                    if self.telegram and hasattr(self.telegram, 'send_alert'):
                        await self.telegram.send_alert(
                            f"🚨 *SL PLACEMENT FAILED*\n\n"
                            f"Symbol: `{symbol}`\n"
                            f"Entry filled @ ₹{ltp:.2f} — SL returned no order ID.\n"
                            f"StopPrice attempted: ₹{stop_price:.2f}\n"
                            f"⚡ Emergency exit triggered. Capital slot released."
                        )
                    await self._emergency_exit(symbol, qty, sl_side)
                    if self.capital:
                        await self.capital.release_slot(broker=self.broker)
                    self._set_exec_cooldown(symbol, reason='SL_NO_ID', seconds=EXEC_COOLDOWN_SECONDS)
                    return None

                logger.info(f"🛡️ SL Placed: {sl_id} @ ₹{stop_price:.2f}")
                self.hard_stops[symbol] = sl_id

                # ── Step 4: Register Position ─────────────────────────────
                pos_state = {
                    'symbol':     symbol,
                    'qty':        qty,
                    'side':       signal_type,
                    'entry_id':   entry_id,
                    'sl_id':      sl_id,
                    'status':     'OPEN',
                    'entry_time': datetime.now(),
                    'entry_price': ltp,
                    'stop_loss':  stop_price,
                    'obs_id':     signal.get('obs_id'),  # Phase 71: ML Link
                    # Phase 51: G13 Targets for trade_manager monitoring
                    'tp_targets': self.compute_take_profits(ltp, signal)
                }

                # Phase 72: Jarvis Broadcast
                get_ml_logger() # Ensure lazy load
                from dashboard_bridge import get_dashboard_bridge
                get_dashboard_bridge().broadcast("ORDER_EVENT", {
                    "symbol": symbol,
                    "type": "ENTRY",
                    "ltp": ltp,
                    "qty": qty,
                    "side": signal_type
                })
                self.active_positions[symbol] = pos_state

                # DB Log
                if self.db:
                    try:
                        await self.db.log_trade_entry({
                            'symbol':    symbol,
                            'direction': side,   # Use 'SELL'/'BUY' from line 490, not 'SHORT'
                            'qty':       qty,
                            'entry_price': ltp,
                            'entry_id':  entry_id   # Phase 93: Pass order ID for dedup
                        })
                    except Exception as db_err:
                        # Non-fatal to execution, but important
                        logger.error(f"❌ [ENTRY-DB] Failed to log entry for {symbol}: {db_err}")

                cap_status = self.capital.get_slot_status() if self.capital else {}
                logger.info(
                    f"✅ [ENTRY COMPLETE] {symbol} {signal_type} ×{qty} @ ₹{ltp:.2f} | "
                    f"SL=₹{stop_price:.2f} | "
                    f"real_margin_used=₹{cap_status.get('real_margin', 0):.2f}"
                )
                return pos_state

            except Exception as e:
                error_msg = str(e)
                logger.error(f"❌ [ENTRY] Exception for {symbol}: {error_msg}")

                # Set cooldown on broker exception
                self._set_exec_cooldown(symbol, reason=f'BROKER_EXCEPTION: {error_msg[:50]}')

                # CRITICAL: Release capital if slot was acquired before this exception
                # (slot is acquired after fill, before SL — so SL exceptions reach here with slot held)
                if self.capital and not self.capital.is_slot_free:
                    try:
                        logger.warning(
                            f"[ENTRY-EXCEPT] Capital slot still occupied during exception "
                            f"for {symbol} — force releasing."
                        )
                        await self.capital.release_slot(broker=self.broker)
                    except Exception as cap_e:
                        logger.error(f"[ENTRY-EXCEPT] Capital release failed: {cap_e}")

                real_margin = self.capital._real_margin if self.capital else 0
                failure_msg = (
                    f"🚨 *ORDER FAILED*\n\n"
                    f"Symbol: `{symbol}` {side}\n"
                    f"Error:  `{error_msg[:200]}`\n\n"
                    f"━━━ Payload ━━━\n"
                    f"Qty:        {qty}\n"
                    f"LTP:        ₹{ltp:.2f}\n"
                    f"Cost:       ₹{required_capital:.2f}\n"
                    f"Margin:     ₹{real_margin:.2f}\n"
                    f"MarginReq:  ₹{margin_req:.2f}\n"
                    f"⏳ Cooldown: 15 min set for {symbol}"
                )
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(failure_msg)
                return None

    # ─────────────────────────────────────────────────────────────────────────
    # EXIT
    # ─────────────────────────────────────────────────────────────────────────

    async def safe_exit(self, symbol: str, reason: str, emergency: bool = False) -> bool:
        """
        Async Safe Exit with WebSocket Race Condition Protection.
        Phase 44.6: _finalize_closed_position now calls release_slot(broker).
        """
        lock = self._get_lock(symbol)

        async with lock:
            if self.exit_in_progress.get(symbol, False):
                logger.warning(f"EXIT_ALREADY_IN_PROGRESS {symbol}")
                return False

            self.exit_in_progress[symbol] = True

            try:
                if symbol not in self.active_positions:
                    logger.warning(f"[EXIT] {symbol} not found active.")
                    return False

                pos = self.active_positions[symbol]
                if pos['status'] != 'OPEN':
                    return False

                logger.info(f"🔻 [EXIT] Initiating Safe Exit for {symbol} ({reason})")
                
                # Phase 52: Cancel all pending orders BEFORE placing exit order
                # Prevents phantom SL from executing AFTER position is closed
                try:
                    loop = asyncio.get_event_loop()
                    rest = getattr(self.broker, 'rest_client', None)
                    if rest:
                        ob = await loop.run_in_executor(None, rest.orderbook)
                        if isinstance(ob, dict) and ob.get('s') == 'ok':
                            for o in ob.get('orderBook', []):
                                if (o.get('symbol') == symbol
                                        and o.get('status') == FYERS_ORDER_STATUS_PENDING):
                                    await self.broker.cancel_order(o['id'])
                                    logger.info(f"[SAFE_EXIT] Cancelled pending order {o['id']} for {symbol}")
                except Exception as e:
                    logger.warning(f"[SAFE_EXIT] Order cleanup failed (non-fatal): {e}")

                pos['status'] = 'CLOSING'

                # STEP 1: CANCEL SL FIRST
                sl_id = pos.get('sl_id') or self.hard_stops.get(symbol)
                if sl_id:
                    logger.info(f"[EXIT] Cancelling SL {sl_id}...")
                    cancelled = await self.broker.cancel_order(sl_id)

                    if cancelled:
                        logger.info(f"✅ SL Cancelled: {sl_id}")
                        if symbol in self.hard_stops:
                            del self.hard_stops[symbol]
                    else:
                        logger.warning(f"⚠️ SL Cancel Failed: {sl_id}")
                        pos_check = await self.broker.get_position(symbol)
                        if pos_check is None:
                            logger.info(f"POSITION_CLOSED_BY_SL {symbol}")
                            await self._finalize_closed_position(
                                symbol=symbol,
                                reason='HARD_STOP_FILLED',
                                exit_price=0.0,
                                pnl=0.0,
                                send_alert=True,
                            )
                            return True
                        if not emergency:
                            return False

                # STEP 2: PLACE EXIT ORDER
                exit_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
                exit_id = await self.broker.place_order(
                    symbol=symbol,
                    side=exit_side,
                    qty=pos['qty'],
                    order_type='MARKET'
                )
                logger.info(f"[EXIT] Exit Order Placed: {exit_id}")

                # STEP 3: WAIT FOR FILL (15s)
                filled = await self.broker.wait_for_fill(exit_id, timeout=15.0)
                if filled:
                    logger.info(f"✅ Exit Filled: {symbol}")
                else:
                    logger.error(f"❌ Exit Not Filled: {symbol}")

                # STEP 4: CLEANUP (releases capital slot + re-syncs margin)
                exit_price = 0.0
                pnl = 0.0
                if filled:
                    try:
                        # Try to get real exit price from broker
                        exit_price = await self.broker.get_order_avg_price(exit_id)
                        if exit_price > 0:
                            entry_price = pos.get('entry_price', 0)
                            qty = pos.get('qty', 0)
                            if pos['side'] == 'SHORT':
                                pnl = (entry_price - exit_price) * qty
                            else:
                                pnl = (exit_price - entry_price) * qty
                    except Exception as e:
                        logger.warning(f"[SAFE_EXIT] Could not fetch real exit price: {e}")

                await self._finalize_closed_position(
                    symbol=symbol,
                    reason=reason,
                    exit_price=exit_price,
                    pnl=pnl,
                    send_alert=False,
                )
                if self.telegram:
                    await self.telegram.send_alert(f"✅ **CLOSED**: {symbol} ({reason})")

                return True

            except Exception as e:
                logger.error(f"❌ [EXIT] Critical Error: {e}")
                return False
            finally:
                self.exit_in_progress[symbol] = False

    async def close_partial_position(self, symbol: str, quantity: int, reason: str) -> dict:
        """
        Phase 52: Closes `quantity` shares of an open short position via market BUY order.
        Returns: {"status": "SUCCESS", "order_id": str, "filled_qty": int}
              or {"status": "FAILED", "error": str}
        
        CRITICAL: Must NEVER call _finalize_closed_position() (G13 isolation).
        """
        lock = self._get_lock(symbol)
        async with lock:
            try:
                if symbol not in self.active_positions:
                    return {"status": "FAILED", "error": f"{symbol} not active"}

                pos = self.active_positions[symbol]
                if pos['qty'] < quantity:
                    return {"status": "FAILED", "error": f"Insufficient qty: {pos['qty']} < {quantity}"}

                # ── IDEMPOTENCY GUARD ──
                # Phase 77: Reject if the same TP level for this symbol was triggered in last 30s
                import time
                now_ts = time.time()
                symbol_partials = self.partial_exits_in_progress.get(symbol, {})
                last_ts = symbol_partials.get(reason, 0)
                if now_ts - last_ts < 30:
                    logger.warning(f"🚫 [PARTIAL EXIT] Duplicate {reason} for {symbol} suppressed (last attempt {now_ts - last_ts:.1f}s ago)")
                    return {"status": "FAILED", "error": "Duplicate request (idempotency)"}

                # Mark as in progress
                if symbol not in self.partial_exits_in_progress:
                    self.partial_exits_in_progress[symbol] = {}
                self.partial_exits_in_progress[symbol][reason] = now_ts

                logger.info(f"🔻 [PARTIAL EXIT] Closing {quantity} shares of {symbol} ({reason})")
                
                exit_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
                exit_id = await self.broker.place_order(
                    symbol=symbol,
                    side=exit_side,
                    qty=quantity,
                    order_type='MARKET'
                )
                
                # Wait for fill (15s)
                filled = await self.broker.wait_for_fill(exit_id, timeout=15.0)
                if filled:
                    logger.info(f"✅ Partial Fill: {symbol} ×{quantity}")
                    # Update internal qty
                    pos['qty'] -= quantity
                    # NOTE: _finalize_closed_position is NOT called here.
                    return {"status": "SUCCESS", "order_id": exit_id, "filled_qty": quantity}
                else:
                    return {"status": "FAILED", "error": "Fill timeout"}

            except Exception as e:
                logger.error(f"❌ [PARTIAL EXIT] Error: {e}")
                return {"status": "FAILED", "error": str(e)}

    async def modify_sl_qty(self, symbol: str, new_qty: int) -> bool:
        """
        Phase 52: After partial close, reduce the SL-M order qty to match remaining position.
        Searches pending orderbook for symbol's SL order and modifies qty.
        Returns True if modified, False if not found or failed.
        """
        try:
            loop = asyncio.get_event_loop()
            rest = getattr(self.broker, 'rest_client', None)
            if not rest:
                logger.error(f"[SL_QTY] No rest_client for {symbol}")
                return False

            # Run blocking REST call in thread
            orderbook = await loop.run_in_executor(None, rest.orderbook)
            if not isinstance(orderbook, dict) or orderbook.get('s') != 'ok':
                return False

            for order in orderbook.get('orderBook', []):
                if (order.get('symbol') == symbol
                        and order.get('status') == FYERS_ORDER_STATUS_PENDING
                        and order.get('side') == 1):   # side=1 = BUY (our SL for a SHORT)
                    order_id = order['id']
                    tick = 0.05
                    stop_price = order.get('stopPrice', 0)
                    limit_price = round(stop_price * 1.005 / tick) * tick  # 0.5% above trigger

                    modify_data = {
                        "id":         order_id,
                        "qty":        new_qty,
                        "type":       4,          # SL-M
                        "limitPrice": round(limit_price, 2),
                        "stopPrice":  stop_price,
                    }
                    resp = await loop.run_in_executor(
                        None,
                        lambda: rest.modify_order(data=modify_data)
                    )
                    if resp and resp.get('s') == 'ok':
                        logger.info(f"[SL_QTY] {symbol} SL qty updated → {new_qty}")
                        return True
                    else:
                        logger.error(f"[SL_QTY] modify failed for {symbol}: {resp}")
                        return False

            logger.warning(f"[SL_QTY] No pending BUY SL order found for {symbol}")
            return False

        except Exception as e:
            logger.error(f"[SL_QTY] Exception for {symbol}: {e}")
            return False

    async def _emergency_exit(self, symbol: str, qty: int, side: str):
        try:
            await self.broker.place_order(symbol=symbol, qty=qty, side=side, order_type='MARKET')
        except Exception as e:
            logger.critical(f"EMERGENCY EXIT FAILED: {e}")
