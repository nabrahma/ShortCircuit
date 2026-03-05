import asyncio
import logging
import json
from datetime import datetime, date, time as dtime
from database import DatabaseManager
from fyers_broker_interface import FyersBrokerInterface

logger = logging.getLogger(__name__)


class ReconciliationEngine:
    """
    Phase 42.3: HFT Reconciliation Engine — Zero-cost when flat, cache-driven when live.

    Architecture:
    - FLAT STATE  → pure cache check, 0 REST calls, 0 DB queries. Sub-millisecond.
    - LIVE STATE  → cache-first broker read, DB read with dirty-flag guard.
    - Dirty flag  → set externally by TradeManager on open/close events.
    """

    def __init__(
        self,
        broker: FyersBrokerInterface,
        db_manager: DatabaseManager,
        telegram_bot,
        capital_manager=None,    # NEW Phase 44.6
        order_manager=None,      # NEW Phase 44.6
    ):
        self.broker         = broker
        self.db             = db_manager
        self.telegram       = telegram_bot
        self.capital        = capital_manager   # NEW
        self.order_manager  = order_manager     # NEW
        self.running        = False

        # ── Internal State Cache ──────────────────────────────────────
        self._db_positions:      dict = {}
        self._db_dirty:          bool = True
        self._has_open_positions: bool = False
        self._shutdown_event:    asyncio.Event = None
        # ─────────────────────────────────────────────────────────────

    # ── Called by TradeManager when trade opens or closes ─────────────
    def mark_dirty(self):
        """
        Call this from TradeManager whenever a trade opens or closes.
        Forces DB re-fetch on next reconciliation cycle.
        """
        self._db_dirty = True
        logger.debug("🔁 Reconciliation marked dirty.")
    # ──────────────────────────────────────────────────────────────────

    async def start(self):
        if self.running:
            return
        asyncio.create_task(self.run())

    async def stop(self):
        """Stop with hard timeout — never hangs more than 10s."""
        self.running = False
        logger.info("[REC-ENGINE] Stop called. Hard timeout: 10s.")
        # Nothing to await currently — stop is immediate once running=False
        logger.info("🛑 Reconciliation Engine Stopped.")

    async def _interruptible_sleep(self, seconds: float):
        """Sleep that wakes immediately when shutdown_event is set."""
        if self._shutdown_event is None:
            await asyncio.sleep(seconds)
            return
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass  # Normal — sleep completed without shutdown

    async def run(self, shutdown_event: asyncio.Event = None):
        if self.running:
            return
        self.running = True
        self._shutdown_event = shutdown_event
        logger.info("✅ Reconciliation Engine Started (WebSocket Mode).")
        while self.running and (shutdown_event is None or not shutdown_event.is_set()):
            start_time = asyncio.get_event_loop().time()
            try:
                await self.reconcile()
            except Exception as e:
                logger.error(f"Reconciliation error: {e}")

            elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000
            interval = self._get_reconciliation_interval()

            if elapsed_ms > 500 and self._is_market_hours():
                logger.warning(f"⚠️ Slow Reconciliation: {elapsed_ms:.3f}ms")
            elif elapsed_ms > 3000:
                logger.warning(f"⚠️ Very Slow Reconciliation (off-hours): {elapsed_ms:.3f}ms")

            await self._interruptible_sleep(interval)

    async def reconcile(self):
        """
        One reconciliation pass.

        FAST PATH (flat):
            - Read broker WebSocket cache directly (0 REST, 0 DB)
            - If cache also shows flat → return immediately
            - Total cost: ~0.1ms

        LIVE PATH (positions open):
            - Broker: WebSocket cache first, REST fallback only if cache is stale
            - DB: only re-query if dirty flag is set
            - Full compare + alert on divergence
        """

        # ── STEP 1: Read broker cache directly (0 cost) ───────────────
        broker_open = self._read_broker_cache()

        # ── FAST PATH: Both sides flat ─────────────────────────────────
        if not broker_open and not self._has_open_positions and not self._db_dirty:
            # Nothing on broker, nothing tracked locally, no recent updates → definitively flat
            return

        # ── LIVE PATH ─────────────────────────────────────────────────
        # Broker has positions OR we think DB has positions

        # Step 2: Get broker positions (cache-first, REST fallback)
        try:
            broker_positions = await self._get_broker_positions_cached(broker_open)
        except Exception as e:
            logger.error(f"Reconcile: Broker fetch failed: {e}")
            return

        # Step 3: Get DB positions (only if dirty)
        try:
            db_positions = await self._get_db_positions_cached()
        except Exception as e:
            logger.error(f"Reconcile: DB fetch failed: {e}")
            return

        # Update master flat/live flag
        self._has_open_positions = bool(db_positions) or bool(broker_positions)

        # Step 4: Compare
        orphans    = []
        phantoms   = []
        mismatched = []

        for symbol, b_pos in broker_positions.items():
            b_qty = b_pos.get('qty', 0)
            if symbol not in db_positions:
                orphans.append({'symbol': symbol, 'qty': b_qty})
            elif db_positions[symbol] != b_qty:
                mismatched.append({
                    'symbol': symbol,
                    'db_qty': db_positions[symbol],
                    'broker_qty': b_qty
                })

        for symbol, db_qty in db_positions.items():
            if symbol not in broker_positions:
                phantoms.append({'symbol': symbol, 'qty': db_qty})

        # Step 5: Alert on divergence
        if orphans or phantoms or mismatched:
            await self._handle_divergence(
                db_positions, broker_positions,
                orphans, phantoms, mismatched
            )

    # ── Private Helpers ───────────────────────────────────────────────

    def _read_broker_cache(self) -> bool:
        """
        Read broker WebSocket position cache directly.
        Returns True if any non-zero qty position exists.
        Zero REST calls. Zero DB calls. ~0.1ms.
        """
        try:
            if not self.broker.position_cache:
                return False
            return any(
                p.net_qty != 0
                for p in self.broker.position_cache.values()
            )
        except Exception:
            return False  # if cache is broken, fall through to live path

    async def _get_broker_positions_cached(self, cache_has_data: bool) -> dict:
        """
        If WebSocket cache has data, build the dict from cache (0 REST).
        Only falls back to REST if cache appears empty but we expect positions.
        """
        if cache_has_data:
            # Build from cache directly — no REST call
            result = {}
            for symbol, p in self.broker.position_cache.items():
                if p.net_qty != 0:
                    result[symbol] = {
                        'qty':       p.net_qty,
                        'symbol':    symbol,
                        'avg_price': getattr(p, 'avg_price', 0.0),   # NEW
                    }
            return result

        # Cache is empty but we might have stale state — hit REST once to verify
        cached_positions = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, self._sync_get_positions
            ),
            timeout=2.0
        )
        return {p['symbol']: p for p in cached_positions}

    def _sync_get_positions(self):
        """Synchronous broker call — runs in executor, doesn't block event loop."""
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(self.broker.get_all_positions())
        finally:
            loop.close()

    async def _get_db_positions_cached(self) -> dict:
        """
        Return cached DB positions unless dirty flag is set.
        DB query only runs when TradeManager signals a change.
        """
        if not self._db_dirty:
            return self._db_positions  # cache hit — 0ms

        # Cache miss — re-fetch
        rows = await asyncio.wait_for(
            self.db.fetch("SELECT symbol, qty FROM positions WHERE state = 'OPEN'"),
            timeout=1.5
        )
        self._db_positions = {row['symbol']: row['qty'] for row in rows}
        self._db_dirty = False  # clear flag until next trade event
        logger.debug(f"🗄️ DB positions refreshed: {len(self._db_positions)} open.")
        return self._db_positions

    async def adopt_orphan(self, broker_pos: dict):
        """
        Phase 44.6: Adopt an orphaned broker position.
        Called by _handle_divergence() when broker has position not in DB/internal registry.
    
        Steps:
          1. Register minimal position in order_manager.active_positions
          2. Place emergency SL at 1% adverse from avg price
          3. Acquire capital slot
          4. Alert Telegram
        """
        symbol    = broker_pos.get('symbol')
        qty       = abs(broker_pos.get('qty', 0))
        net_qty   = broker_pos.get('qty', 0)
        side      = 'SHORT' if net_qty < 0 else 'LONG'
        avg_price = broker_pos.get('avg_price', 0.0)
    
        if not symbol or qty == 0:
            logger.error(f"[ADOPT] Cannot adopt — invalid broker_pos: {broker_pos}")
            return
    
        try:
            # Step 1: Register in order_manager internal state
            if self.order_manager:
                sl_pct   = 0.01   # emergency 1% SL
                sl_price = round(avg_price * (1 + sl_pct), 2) if side == 'SHORT' \
                           else round(avg_price * (1 - sl_pct), 2)
                sl_side  = 'BUY' if side == 'SHORT' else 'SELL'
    
                # Step 2: Place emergency SL order
                try:
                    sl_id = await self.broker.place_order(
                        symbol=symbol,
                        side=sl_side,
                        qty=qty,
                        order_type='SL_MARKET',
                        trigger_price=sl_price,
                    )
                    logger.critical(
                        f"[ADOPT] Emergency SL placed for {symbol} | "
                        f"sl_id={sl_id} sl_price=₹{sl_price:.2f}"
                    )
                except Exception as e:
                    sl_id = None
                    logger.critical(
                        f"[ADOPT] Emergency SL FAILED for {symbol}: {e} | "
                        f"POSITION IS NAKED — manual intervention required"
                    )
                    if self.telegram:
                        await self.telegram.send_alert(
                            f"🚨 *ORPHAN SL FAILED*\n\n"
                            f"Symbol: `{symbol}` {side} ×{qty}\n"
                            f"SL placement failed: `{e}`\n"
                            f"⚠️ **Manual close required NOW**"
                        )
    
                # Register minimal position state
                self.order_manager.active_positions[symbol] = {
                    'symbol':      symbol,
                    'qty':         qty,
                    'side':        side,
                    'entry_id':    'ORPHAN_ADOPTED',
                    'sl_id':       sl_id,
                    'status':      'OPEN',
                    'entry_time':  datetime.utcnow(),
                    'entry_price': avg_price,
                    'stop_loss':   sl_price if sl_id else 0.0,
                    'source':      'ORPHAN_ADOPTED',
                }
                if sl_id:
                    self.order_manager.hard_stops[symbol] = sl_id
    
            # Step 3: Acquire capital slot
            if self.capital:
                try:
                    if self.capital.is_slot_free:
                        await self.capital.acquire_slot(symbol)
                    else:
                        logger.warning(
                            f"[ADOPT] Capital slot already occupied by "
                            f"{self.capital.active_symbol} — cannot acquire for {symbol}"
                        )
                except Exception as e:
                    logger.error(f"[ADOPT] Capital acquire_slot failed: {e}")
    
            # Step 4: Alert
            sl_msg = f"₹{sl_price:.2f}" if sl_id else "FAILED ⚠️"
            alert = (
                f"🚨 *ORPHAN ADOPTED*\n\n"
                f"Symbol:    `{symbol}`\n"
                f"Side:      {side}\n"
                f"Qty:       {qty}\n"
                f"AvgPrice:  ₹{avg_price:.2f}\n"
                f"EmergSL:   {sl_msg}\n\n"
                f"Position was not tracked internally.\n"
                f"Bot has now adopted it and placed an SL."
            )
            if self.telegram:
                await self.telegram.send_alert(alert)
    
            logger.critical(
                f"[ADOPT] ✅ Orphan adopted: {symbol} {side} ×{qty} "
                f"@ ₹{avg_price:.2f} | emergency_sl={sl_id}"
            )
    
        except Exception as e:
            logger.critical(f"[ADOPT] ADOPTION FAILED for {symbol}: {e}")
            if self.telegram:
                await self.telegram.send_alert(
                    f"🔥 *ORPHAN ADOPTION FAILED*\n`{symbol}`\n`{e}`"
                )

    async def _handle_divergence(self, db_pos, broker_pos, orphans, phantoms, mismatched):
        """
        Phase 44.6: Detect + ACT on state divergence.
        Previous version: alert only.
        Now: adopts orphans, releases phantom capital slots.
        """
        logger.critical(
            f"🚨 DISCREPANCY: Orphans={len(orphans)}, "
            f"Phantoms={len(phantoms)}, Mismatch={len(mismatched)}"
        )
    
        # ── DB log ────────────────────────────────────────────────────────
        try:
            await self.db.execute("""
                INSERT INTO reconciliation_log (
                    timestamp, internal_position_count, broker_position_count,
                    orphaned_positions, phantom_positions, quantity_mismatches,
                    status, session_date, check_duration_ms
                ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, 0)
            """,
                len(db_pos), len(broker_pos),
                json.dumps(orphans), json.dumps(phantoms), json.dumps(mismatched),
                'DIVERGENCE_DETECTED', date.today()
            )
        except Exception as e:
            logger.error(f"Failed to log reconciliation discrepancy: {e}")
    
        # ── ORPHANS: broker has position, internal state doesn't ─────────
        for orphan in orphans:
            logger.critical(
                f"🚨 ORPHAN DETECTED: {orphan['symbol']} qty={orphan['qty']} — "
                f"adopting with emergency SL"
            )
            # Build broker_pos dict for adopt_orphan
            adopt_data = {
                'symbol':    orphan['symbol'],
                'qty':       orphan['qty'],
                'avg_price': broker_pos.get(orphan['symbol'], {}).get('avg_price', 0.0),
            }
            await self.adopt_orphan(adopt_data)
    
        # ── PHANTOMS: internal state has position, broker doesn't ────────
        for phantom in phantoms:
            sym = phantom['symbol']
            logger.critical(
                f"👻 GHOST POSITION: {sym} — broker is flat but internal registry "
                f"says open. Force-closing internal record."
            )
            # Clean order_manager state
            if hasattr(self, 'order_manager') and self.order_manager:
                self.order_manager.active_positions.pop(sym, None)
                self.order_manager.hard_stops.pop(sym, None)
                self.order_manager.exit_in_progress.pop(sym, None)
    
            # Release capital slot if this ghost was holding it
            if hasattr(self, 'capital') and self.capital and self.capital.active_symbol == sym:
                try:
                    await self.capital.release_slot(broker=self.broker)
                    logger.info(f"[GHOST] Capital slot released for phantom {sym}")
                except Exception as e:
                    logger.error(f"[GHOST] Capital release failed for {sym}: {e}")
    
            if self.telegram:
                await self.telegram.send_alert(
                    f"👻 *GHOST POSITION CLEARED*\n\n"
                    f"Symbol: `{sym}`\n"
                    f"Internal registry said OPEN but broker is flat.\n"
                    f"State cleaned. Capital slot released."
                )
    
        # ── MISMATCHED: qty differs ───────────────────────────────────────
        for mm in mismatched:
            logger.critical(
                f"⚠️ QTY MISMATCH: {mm['symbol']} "
                f"db_qty={mm['db_qty']} broker_qty={mm['broker_qty']}"
            )
            if self.telegram:
                await self.telegram.send_alert(
                    f"⚠️ *QTY MISMATCH*\n\n"
                    f"Symbol: `{mm['symbol']}`\n"
                    f"Internal: {mm['db_qty']} | Broker: {mm['broker_qty']}\n"
                    f"Manual review required."
                )

    def _get_reconciliation_interval(self) -> int:
        if self._is_market_hours():
            return 6
        return 30 if self._has_open_positions else 300

    def _is_market_hours(self) -> bool:
        try:
            import pytz
            IST = pytz.timezone('Asia/Kolkata')
            now = datetime.now(IST)
        except Exception:
            now = datetime.now()
        if now.weekday() >= 5:
            return False
        return dtime(9, 15) <= now.time() <= dtime(15, 30)
