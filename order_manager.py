import asyncio
import json
import logging
import math
import uuid
from datetime import datetime
from typing import Dict, Optional, Any
from fyers_broker_interface import FyersBrokerInterface

logger = logging.getLogger(__name__)

# FYERS orderbook status code used for filled/traded orders.
# Sources:
# 1) FYERS API Connect docs (Transaction/Orderbook section): https://myapi.fyers.in/docsv3/#tag/Transaction-Info
# 2) FYERS community status-code mapping thread: https://fyers.in/community/questions-5gz5j8db/post/what-are-the-order-status-codes-in-fyers-api-v3-orderbook-response-FbEQ31ykj5fN7WJ
FYERS_ORDER_STATUS_TRADED = 2

class OrderManager:
    """
    Phase 42.2: Async Order Manager with WebSocket Support.
    
    Responsibilities:
    1. Async Execution via FyersBrokerInterface
    2. Zero-latency Fill Detection (WebSocket)
    3. Safe Entry with Immediate Hard Stop (SL-M)
    4. Phantom Fill Prevention (Cancel SL *before* Exit)
    5. Per-Symbol Locking
    """
    
    def __init__(
        self, 
        broker: FyersBrokerInterface, 
        telegram_bot, 
        db=None, 
        capital_manager=None
    ):
        self.broker = broker
        self.telegram = telegram_bot
        self.db = db
        self.capital = capital_manager
        
        # State
        self.active_positions: Dict[str, Any] = {}
        self.position_locks: Dict[str, asyncio.Lock] = {}
        self.exit_in_progress: Dict[str, bool] = {}
        self.hard_stops: Dict[str, str] = {} # symbol -> sl_order_id

    def _get_lock(self, symbol: str) -> asyncio.Lock:
        if symbol not in self.position_locks:
            self.position_locks[symbol] = asyncio.Lock()
        return self.position_locks[symbol]

    async def _finalize_closed_position(
        self,
        symbol: str,
        reason: str,
        exit_price: float = 0.0,
        pnl: float = 0.0,
        send_alert: bool = False,
    ) -> None:
        """
        Shared close-path cleanup to keep state, capital, and DB in sync.
        """
        self.active_positions.pop(symbol, None)
        self.hard_stops.pop(symbol, None)
        self.exit_in_progress.pop(symbol, None)

        if self.capital:
            try:
                self.capital.release(symbol)
            except Exception as e:
                logger.error(f"[CLOSE] Capital release failed for {symbol}: {e}")

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

    async def monitor_hard_stop_status(self, symbol: str) -> bool:
        """
        Hard-stop monitor used by FocusEngine's sync loop.
        Detects SL fill from broker orderbook and clears local position state.
        Returns True when hard-stop fill is detected and state is closed.
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
                    # Legacy/mock fallback used in tests.
                    orderbook = await loop.run_in_executor(None, self.broker.orderbook)

                if not isinstance(orderbook, dict) or orderbook.get('s') != 'ok':
                    return False

                for order in orderbook.get('orderBook', []):
                    if str(order.get('id')) != str(sl_id):
                        continue

                    if order.get('status') == FYERS_ORDER_STATUS_TRADED:
                        # Status 2 is treated as traded/filled per FYERS v3 references.
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

    async def get_today_trades(self) -> list:
        """Returns today's trades and PnL using the Fyers positions API."""
        try:
            positions = await self.broker.get_all_positions()
            trades = []
            for p in positions:
                trades.append({
                    'symbol': p.get('symbol', 'UNKNOWN'),
                    'realised_pnl': float(p.get('realized_profit', 0)),
                    'unrealised_pnl': float(p.get('unrealized_profit', 0)),
                    'qty': p.get('netQty', 0)
                })
            return trades
        except Exception as e:
            logger.error(f"Error fetching today's trades: {e}")
            return []

    async def startup_reconciliation(self):
        """
        Runs at startup to sync state.
        Uses WebSocket cache for instant position check.
        """
        import time
        start_time = time.time()
        logger.info("🔍 [STARTUP] Running Async Order Reconciliation...")
        try:
            # DB Pool Warmup (Phase 42.4 Fix #7)
            if self.db:
                try:
                    pool = await self.db.get_pool()
                    async with pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                    logger.info("DB Pool warmed up.")
                except Exception as e:
                    logger.warning(f"DB Pool warmup failed: {e}")

            # 1. Fetch Positions (Uses Cache/REST transparently)
            open_positions = await self.broker.get_all_positions()
            
            for pos in open_positions:
                qty = pos.get('qty', 0)
                symbol = pos.get('symbol')
                if qty != 0:
                    logger.critical(f"⚠️ [STARTUP] ORPHAN FOUND: {symbol} Qty: {qty}")
                    if self.telegram:
                        await self.telegram.send_alert(f"⚠️ **ORPHAN**: {symbol} ({qty})")
                    
                    # TODO: Import into active_positions?
                    # For Phase 42.2, just alert and let human decide.

            # 2. Cancel Pending Orders (Uses REST for safety on startup)
            # We don't have a specific get_all_orders in broker yet, so we use rest_client directly
            # or add it to broker. For now, rely on manual intervention or implementation in broker.
            # But the PRD didn't specify get_all_orders in broker.
            # We will use the broker's underlying rest_client for this specific maintenance task if needed,
            # but ideally we should add it to the interface.
            # For now, let's skip auto-cancellation to avoid complexity, or implement it if critical.
            # The original code did it.
            
            # Use run_in_executor for the direct rest call if needed, 
            # BUT the broker.rest_client is available.
            loop = asyncio.get_event_loop()
            orderbook = await loop.run_in_executor(None, self.broker.rest_client.orderbook)
            
            if orderbook and isinstance(orderbook, dict) and orderbook.get('s') == 'ok':
                pending = [o for o in orderbook.get('orderBook', []) if o['status'] == 6] # 6 = Pending
                for order in pending:
                    logger.info(f"[STARTUP] Cancelling stale order {order['id']}")
                    await self.broker.cancel_order(order['id'])
            
            elapsed_ms = (time.time() - start_time) * 1000
            
            if elapsed_ms > 3000:
                logger.error(f"CRITICAL Slow Reconciliation {elapsed_ms:.0f}ms — possible position state lag")
                if self.telegram:
                    await self.telegram.send_alert(f"⚠️ Reconciliation lag {elapsed_ms:.0f}ms — check positions manually")
            elif elapsed_ms > 1500:
                logger.error(f"Slow Reconciliation {elapsed_ms:.0f}ms")
            elif elapsed_ms > 500:
                logger.warning(f"Slow Reconciliation {elapsed_ms:.0f}ms")
                
            logger.info("✅ [STARTUP] Reconciliation Done.")
            
        except Exception as e:
            logger.critical(f"🔥 [STARTUP] Failed: {e}")

    async def enter_position(self, signal: dict) -> Optional[dict]:
        """
        Async Entry + SL-M using WebSocket Fill Detection.
        Phase 44.4: Full execution observability.
        """
        symbol = signal['symbol']
        lock = self._get_lock(symbol)
        
        async with lock:
            logger.info(f"🚀 [ENTRY] Processing {symbol}...")
            
            # =========================================================
            # SECONDARY AUTO MODE GATE (Defense in Depth)
            # =========================================================
            if self.telegram and hasattr(self.telegram, 'is_auto_mode'):
                if not self.telegram.is_auto_mode():
                    logger.critical(
                        f"🚫 ORDER BLOCKED: enter_position called while auto_mode=False. "
                        f"Signal: {symbol}. "
                        f"This is a bug — focus_engine should have caught this."
                    )
                    return None
            # =========================================================

            # ── PHASE 44.4: CAPITAL-AWARE SIZING ──────────────────────
            # Get real buying power from capital_manager (fixes code -50 root cause)
            buying_power = 9000  # Fallback default
            if self.capital:
                cap_status = self.capital.get_status()
                buying_power = cap_status.get('available', 9000)
            
            ltp = signal.get('ltp', 0)
            if ltp == 0: ltp = await self.broker.get_ltp(symbol) or 0
            if ltp == 0:
                logger.error(f"❌ [ENTRY] {symbol}: LTP is 0, cannot size position")
                return None
            
            # Calculate quantity with breakdown
            raw_qty = buying_power / ltp
            qty = int(math.floor(raw_qty))
            required_capital = qty * ltp
            
            logger.info(
                f"[SIZING] {symbol}: buying_power=₹{buying_power:.2f} / "
                f"ltp=₹{ltp:.2f} = {raw_qty:.4f} → floor → qty={qty} "
                f"(cost=₹{required_capital:.2f})"
            )
            
            # ── QTY ZERO GUARD ────────────────────────────────────────
            if qty == 0:
                msg = (
                    f"🚫 *ORDER BLOCKED — QTY ZERO*\n\n"
                    f"Symbol:  `{symbol}`\n"
                    f"LTP:     ₹{ltp:.2f}\n"
                    f"Capital: ₹{buying_power:.2f}\n\n"
                    f"Stock too expensive for available buying power.\n"
                    f"Need minimum ₹{ltp:.2f} per share."
                )
                logger.warning(f"❌ [ENTRY] {symbol}: qty=0 (₹{buying_power:.2f} / ₹{ltp:.2f})")
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(msg)
                return None
            
            # ── OVER-MARGIN PRE-FLIGHT CHECK ──────────────────────────
            if required_capital > buying_power:
                msg = (
                    f"🚫 *ORDER BLOCKED — OVER MARGIN*\n\n"
                    f"Symbol:   `{symbol}`\n"
                    f"Qty:      {qty} × ₹{ltp:.2f} = ₹{required_capital:.2f}\n"
                    f"Available: ₹{buying_power:.2f}\n"
                    f"Shortfall: ₹{required_capital - buying_power:.2f}\n\n"
                    f"⚠️ This would have caused Fyers `code -50`."
                )
                logger.error(f"🚫 [ENTRY] {symbol}: over-margin ₹{required_capital:.2f} > ₹{buying_power:.2f}")
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(msg)
                return None
            
            signal_type = signal.get('signal_type', 'SHORT')
            side = 'SELL' if signal_type == 'SHORT' else 'BUY'
            
            # ── PRE-EXECUTION PAYLOAD LOG ─────────────────────────────
            order_payload = {
                'symbol': symbol,
                'side': side,
                'qty': qty,
                'order_type': 'MARKET',
                'productType': 'INTRADAY',
                'ltp_at_signal': ltp,
                'buying_power': buying_power,
                'required_capital': required_capital,
                'sizing_calc': f"floor({buying_power:.2f} / {ltp:.2f}) = floor({raw_qty:.4f}) = {qty}"
            }
            logger.debug(f"[PRE-EXEC] Entry payload: {json.dumps(order_payload, indent=2)}")
            logger.info(f"[PRE-EXEC] {symbol} {side} qty={qty} @ ₹{ltp:.2f} (₹{required_capital:.2f})")
            
            try:
                # 1. Place Entry Order
                entry_id = await self.broker.place_order(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    order_type='MARKET'
                )
                
                # ── POST-SUCCESS: Order ID Confirmation ───────────────
                logger.info(f"✅ Entry Placed: {entry_id} | {symbol} {side} x{qty}")
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(
                        f"✅ *ENTRY ORDER PLACED*\n\n"
                        f"Symbol: `{symbol}` {side}\n"
                        f"Qty: {qty} × ₹{ltp:.2f}\n"
                        f"Order ID: `{entry_id}`"
                    )
                
                # 2. Wait for Fill (WebSocket Push)
                filled = await self.broker.wait_for_fill(entry_id, timeout=30.0)
                if not filled:
                    logger.error(f"❌ Entry Timeout/Failed: {entry_id}")
                    await self.broker.cancel_order(entry_id)
                    if self.telegram and hasattr(self.telegram, 'send_alert'):
                        await self.telegram.send_alert(
                            f"❌ *ENTRY FILL TIMEOUT*\n\n"
                            f"Symbol: `{symbol}`\n"
                            f"Order ID: `{entry_id}`\n"
                            f"Action: Order cancelled."
                        )
                    return None
                
                # 3. Get actual fill price
                fill_price = ltp  # Fallback
                status = await self.broker.get_order_status(entry_id)
                
                # 4. Place SL-M (Immediate)
                sl_pct = 0.02
                stop_price = round(ltp * (1 + sl_pct), 2) if side == 'SELL' else round(ltp * (1 - sl_pct), 2)
                
                # SL Side is opposite to Entry Side
                sl_side = 'BUY' if side == 'SELL' else 'SELL'
                
                sl_id = await self.broker.place_order(
                    symbol=symbol,
                    side=sl_side,
                    qty=qty,
                    order_type='SL_MARKET',
                    trigger_price=stop_price
                )
                
                if not sl_id:
                    logger.critical("🚨 SL PLACEMENT FAILED! EXITING NOW!")
                    if self.telegram and hasattr(self.telegram, 'send_alert'):
                        await self.telegram.send_alert(
                            f"🚨 *SL PLACEMENT FAILED*\n\n"
                            f"Symbol: `{symbol}`\n"
                            f"Entry filled but stop loss could not be placed.\n"
                            f"⚡ Emergency exit triggered."
                        )
                    await self._emergency_exit(symbol, qty, sl_side)
                    return None

                logger.info(f"🛡️ SL Placed: {sl_id} @ ₹{stop_price:.2f}")
                self.hard_stops[symbol] = sl_id
                
                # 5. Register Position
                pos_state = {
                    'symbol': symbol,
                    'qty': qty,
                    'side': signal_type,
                    'entry_id': entry_id,
                    'sl_id': sl_id,
                    'status': 'OPEN',
                    'entry_time': datetime.now(),
                    'entry_price': ltp,
                    'stop_loss': stop_price,
                }
                self.active_positions[symbol] = pos_state
                
                # DB Log
                if self.db:
                    await self.db.log_trade_entry({
                        'symbol': symbol,
                        'direction': signal_type,
                        'qty': qty,
                        'entry_price': ltp
                    })

                return pos_state
                
            except Exception as e:
                # ── POST-FAILURE: Telegram Alert ──────────────────────
                error_msg = str(e)
                logger.error(f"❌ [ENTRY] Exception for {symbol}: {error_msg}")
                
                # Determine suspected field from error pattern
                suspected = "Unknown"
                if "Invalid input" in error_msg or "code -50" in error_msg.lower():
                    if qty == 0:
                        suspected = "qty (calculated as 0)"
                    elif required_capital > buying_power:
                        suspected = "qty × price exceeds margin"
                    else:
                        suspected = "productType or offlineOrder type"
                
                failure_msg = (
                    f"🚨 *ORDER FAILED*\n\n"
                    f"Symbol: `{symbol}` {side}\n"
                    f"Error:  `{error_msg[:200]}`\n\n"
                    f"━━━ Payload ━━━\n"
                    f"Qty:     {qty}\n"
                    f"LTP:     ₹{ltp:.2f}\n"
                    f"Capital: ₹{required_capital:.2f}\n"
                    f"Avail:   ₹{buying_power:.2f}\n"
                    f"Sizing:  floor({buying_power:.0f}/{ltp:.2f})={qty}\n\n"
                    f"🔍 Suspected: {suspected}"
                )
                
                if self.telegram and hasattr(self.telegram, 'send_alert'):
                    await self.telegram.send_alert(failure_msg)
                    
                return None


    async def safe_exit(self, symbol: str, reason: str, emergency: bool = False) -> bool:
        """
        Async Safe Exit with WebSocket Race Condition Protection.
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
                if pos['status'] != 'OPEN': return False
                
                logger.info(f"🔻 [EXIT] Initiating Safe Exit for {symbol} ({reason})")
                pos['status'] = 'CLOSING'
                
                # STEP 1: CANCEL SL FIRST
                sl_id = pos.get('sl_id')
                # Also check hard_stops
                if not sl_id and symbol in self.hard_stops:
                    sl_id = self.hard_stops[symbol]
                
                if sl_id:
                    logger.info(f"[EXIT] Cancelling SL {sl_id}...")
                    cancelled = await self.broker.cancel_order(sl_id)
                    
                    if cancelled:
                        logger.info(f"✅ SL Cancelled: {sl_id}")
                        if symbol in self.hard_stops: del self.hard_stops[symbol]
                    else:
                        logger.warning(f"⚠️ SL Cancel Failed: {sl_id}")
                        # Check if already filled
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
                            return False # Unsafe to proceed if SL status unknown

                # STEP 2: PLACE EXIT ORDER
                exit_side = 'BUY' if pos['side'] == 'SHORT' else 'SELL'
                
                exit_id = await self.broker.place_order(
                    symbol=symbol,
                    side=exit_side,
                    qty=pos['qty'],
                    order_type='MARKET'
                )
                
                logger.info(f"[EXIT] Exit Order Placed: {exit_id}")
                
                # STEP 3: WAIT FOR FILL
                filled = await self.broker.wait_for_fill(exit_id, timeout=30.0)
                
                if filled:
                    logger.info(f"✅ Exit Filled: {symbol}")
                else:
                    logger.error(f"❌ Exit Not Filled: {symbol}")
                    # Logic to handle stuck exit?
                
                # STEP 4: CLEANUP
                await self._finalize_closed_position(
                    symbol=symbol,
                    reason=reason,
                    exit_price=0.0,
                    pnl=0.0,
                    send_alert=False,
                )
                if self.telegram:
                    await self.telegram.send_alert(f"✅ **CLOSED**: {symbol}")
                
                return True

            except Exception as e:
                logger.error(f"❌ [EXIT] Critical Error: {e}")
                return False
            finally:
                self.exit_in_progress[symbol] = False
                
    async def _emergency_exit(self, symbol, qty, side):
        try:
            # Side is already 'BUY' or 'SELL' string from caller? 
            # In enter_position caller passed 'BUY'/'SELL' correctly.
            await self.broker.place_order(symbol=symbol, qty=qty, side=side, order_type='MARKET')
        except Exception as e:
            logger.critical(f"EMERGENCY EXIT FAILED: {e}")
