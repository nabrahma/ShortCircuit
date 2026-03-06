import asyncio
import logging
import json
from datetime import datetime, date, time as dtime
import math
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
        Phase 44.9.3: Adopt an orphaned broker position (manual trade detection).

        Called when broker has a position not tracked internally.
        Fires within 6 seconds of your manual entry during market hours.

        Steps:
          1. Idempotency guard — skip if already adopted
          2. Compute tick-safe SL price
          3. Place emergency SL with tick rounding (same as _round_sl_to_tick)
          4. Register in order_manager.active_positions + hard_stops
          5. Log to DB via db.log_trade_entry() — CRITICAL: prevents infinite re-detection
          6. Set _db_dirty = True — forces fresh DB fetch next cycle
          7. Acquire capital slot (or emit CRITICAL alert if slot occupied)
          8. Send Telegram MANUAL ENTRY ADOPTED alert
        """
        symbol    = broker_pos.get('symbol')
        qty       = abs(broker_pos.get('qty', 0))
        net_qty   = broker_pos.get('qty', 0)
        side      = 'SHORT' if net_qty < 0 else 'LONG'
        avg_price = broker_pos.get('avg_price', 0.0)

        if not symbol or qty == 0:
            logger.error(f"[ADOPT] Cannot adopt — invalid broker_pos: {broker_pos}")
            return

        # ── IDEMPOTENCY GUARD ─────────────────────────────────────────────────
        # If symbol already registered, a prior adoption cycle completed successfully.
        # Do not place another SL or overwrite state.
        if self.order_manager and symbol in self.order_manager.active_positions:
            logger.debug(f"[ADOPT] {symbol} already in active_positions — no-op.")
            return

        logger.critical(
            f"[ADOPT] 🚨 MANUAL ENTRY DETECTED: {symbol} {side} ×{qty} "
            f"@ avg ₹{avg_price:.2f} — starting adoption."
        )

        try:
            # ── Step 1: Compute tick-safe SL price ───────────────────────────
            sl_pct  = 0.01   # emergency 1% SL for adopted orphan
            raw_sl  = (
                avg_price * (1 + sl_pct) if side == 'SHORT'
                else avg_price * (1 - sl_pct)
            )
            # Round away from entry (same logic as OrderManager._round_sl_to_tick)
            if side == 'SHORT':
                sl_price = round(math.ceil(raw_sl / 0.05) * 0.05, 2)
            else:
                sl_price = round(math.floor(raw_sl / 0.05) * 0.05, 2)

            sl_side = 'BUY' if side == 'SHORT' else 'SELL'

            logger.info(
                f"[ADOPT] SL calc: raw=₹{raw_sl:.4f} → tick_rounded=₹{sl_price:.2f}"
            )

            # ── Step 2: Place emergency SL order ─────────────────────────────
            sl_id = None
            try:
                sl_id = await self.broker.place_order(
                    symbol=symbol,
                    side=sl_side,
                    qty=qty,
                    order_type='SL_MARKET',
                    trigger_price=sl_price,
                )
                logger.critical(
                    f"[ADOPT] ✅ Emergency SL placed: {symbol} | "
                    f"sl_id={sl_id} | stop=₹{sl_price:.2f}"
                )
            except Exception as e:
                sl_id = None
                logger.critical(
                    f"[ADOPT] ❌ Emergency SL FAILED for {symbol}: {e} | "
                    f"POSITION IS NAKED — manual close required immediately"
                )
                if self.telegram:
                    await self.telegram.send_alert(
                        f"🚨 *ORPHAN SL FAILED*\n\n"
                        f"Symbol: `{symbol}` {side} ×{qty}\n"
                        f"Avg: ₹{avg_price:.2f} | SL attempted: ₹{sl_price:.2f}\n"
                        f"Error: `{str(e)[:100]}`\n"
                        f"⚠️ **Position is NAKED. Close manually NOW.**"
                    )

            # ── Step 3: Register in order_manager internal state ──────────────
            if self.order_manager:
                self.order_manager.active_positions[symbol] = {
                    'symbol':      symbol,
                    'qty':         qty,
                    'side':        side,
                    'entry_id':    'MANUAL_ENTRY',
                    'sl_id':       sl_id,
                    'status':      'OPEN',
                    'entry_time':  datetime.utcnow(),
                    'entry_price': avg_price,
                    'stop_loss':   sl_price if sl_id else 0.0,
                    'source':      'MANUAL_ENTRY_ADOPTED',
                }
                if sl_id:
                    self.order_manager.hard_stops[symbol] = sl_id
                logger.info(f"[ADOPT] Position registered in active_positions: {symbol}")

            # ── Step 4: Log to DB ─────────────────────────────────────────────
            # CRITICAL: This is what stops infinite re-detection.
            # Without this, every reconcile cycle re-detects the same orphan.
            if self.order_manager and self.order_manager.db:
                try:
                    logger.info("[ADOPT-DB] Using ordermanager.db path (primary)")
                    await self.order_manager.db.log_trade_entry({
                        'symbol':      symbol,
                        'direction':   side,
                        'qty':         qty,
                        'entry_price': avg_price,
                        'order_id':    'MANUAL_ENTRY',
                        'sl_id':       sl_id,
                        'source':      'ORPHAN_RECOVERY',
                        'session_date': date.today(),
                    })
                    logger.info(f"[ADOPT] DB entry logged for {symbol} (state=OPEN)")
                except Exception as e:
                    logger.error(
                        f"[ADOPT-DB] ordermanager.db path FAILED: {e} — "
                        f"orphan may be re-detected next cycle"
                    )
            elif self.db_manager:
                try:
                    logger.info("[ADOPT-DB] Using self.db_manager path (fallback)")
                    await self.db_manager.log_trade_entry({
                        'symbol':      symbol,
                        'direction':   side,
                        'qty':         qty,
                        'entry_price': avg_price,
                        'order_id':    'MANUAL_ENTRY',
                        'sl_id':       sl_id,
                        'source':      'ORPHAN_RECOVERY',
                        'session_date': date.today(),
                    })
                    logger.info(f"[ADOPT] DB entry logged for {symbol} (state=OPEN) via db_manager.")
                except Exception as e:
                    logger.error(f"[ADOPT-DB] self.db_manager path FAILED: {e}")
            elif self.db:
                # Fallback: use reconciliation engine's own db reference
                try:
                    await self.db.execute(
                        """INSERT INTO positions (symbol, direction, qty, entry_price, state, opened_at)
                           VALUES ($1, $2, $3, $4, 'OPEN', NOW())
                           ON CONFLICT (symbol) DO UPDATE SET state='OPEN'""",
                        symbol, side, qty, avg_price
                    )
                    logger.info(f"[ADOPT] DB entry inserted (fallback path) for {symbol}")
                except Exception as e:
                    logger.error(f"[ADOPT] DB fallback insert failed: {e}")

            # ── Step 5: Mark DB dirty ─────────────────────────────────────────
            # Forces fresh DB read next reconcile cycle.
            # After fresh read, symbol will appear in db_positions → no longer an orphan.
            self._db_dirty = True
            logger.info(f"[ADOPT] _db_dirty set True for {symbol}")

            # ── Step 6: Acquire capital slot ──────────────────────────────────
            if self.capital:
                if self.capital.is_slot_free:
                    try:
                        await self.capital.acquire_slot(symbol)
                        logger.info(f"[ADOPT] ✅ Capital slot acquired for {symbol}")
                    except Exception as e:
                        logger.error(f"[ADOPT] acquire_slot failed: {e}")
                else:
                    # TWO POSITIONS OPEN: bot trade + manual trade simultaneously.
                    # Capital slot is held by bot's trade. Manual trade is unprotected.
                    # This is a dangerous state — operator MUST know immediately.
                    existing = self.capital.active_symbol
                    logger.critical(
                        f"[ADOPT] ⚠️ TWO POSITIONS OPEN: capital slot held by {existing}, "
                        f"cannot acquire for manual entry {symbol}. "
                        f"Manual trade is running WITHOUT capital tracking."
                    )
                    if self.telegram:
                        await self.telegram.send_alert(
                            f"🚨 *TWO POSITIONS OPEN — CRITICAL*\n\n"
                            f"Bot trade:    `{existing}` (capital slot held)\n"
                            f"Manual trade: `{symbol}` {side} ×{qty}\n\n"
                            f"Capital slot CANNOT be acquired for manual trade.\n"
                            f"⚠️ Manual trade has an SL but NO capital tracking.\n"
                            f"When `{existing}` closes, bot may enter a 3rd trade.\n\n"
                            f"**Recommended:** Close one position manually."
                        )

            # ── Step 7: Final Telegram alert ──────────────────────────────────
            sl_status = f"₹{sl_price:.2f} (id: {sl_id})" if sl_id else "FAILED ⚠️ Close manually!"
            cap_status = "✅ Acquired" if (self.capital and not self.capital.is_slot_free and
                                            self.capital.active_symbol == symbol) else "⚠️ Slot occupied by other trade"

            if self.telegram:
                await self.telegram.send_alert(
                    f"🤝 *MANUAL ENTRY ADOPTED*\n\n"
                    f"Symbol:    `{symbol}`\n"
                    f"Side:      {side}\n"
                    f"Qty:       {qty}\n"
                    f"AvgPrice:  ₹{avg_price:.2f}\n"
                    f"EmergSL:   {sl_status}\n"
                    f"Capital:   {cap_status}\n\n"
                    f"Bot is now tracking this position.\n"
                    f"SL will fire automatically. Exit via Fyers or let SL hit."
                )

            logger.critical(
                f"[ADOPT] ✅ ADOPTION COMPLETE: {symbol} {side} ×{qty} "
                f"@ ₹{avg_price:.2f} | sl={sl_id} sl_price=₹{sl_price:.2f}"
            )

        except Exception as e:
            logger.critical(f"[ADOPT] ADOPTION FAILED for {symbol}: {e}", exc_info=True)
            if self.telegram:
                await self.telegram.send_alert(
                    f"🔥 *ORPHAN ADOPTION FAILED*\n`{symbol}`\n`{str(e)[:150]}`\n"
                    f"Manual intervention required."
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
            sym = orphan['symbol']

            # IDEMPOTENCY GUARD: if already in active_positions, do not re-adopt.
            # This prevents double-adoption when two reconcile cycles fire close together.
            if (self.order_manager and
                    sym in self.order_manager.active_positions):
                logger.debug(
                    f"[ORPHAN] {sym} already in active_positions — skipping re-adoption."
                )
                continue

            logger.critical(
                f"🚨 ORPHAN DETECTED: {sym} qty={orphan['qty']} — "
                f"adopting with emergency SL"
            )
            # Build broker_pos dict for adopt_orphan
            adopt_data = {
                'symbol':    sym,
                'qty':       orphan['qty'],
                'avg_price': broker_pos.get(sym, {}).get('avg_price', 0.0),
            }
            await self.adopt_orphan(adopt_data)
    
        # ── PHANTOMS: internal state has position, broker doesn't ────────
        # This fires when YOU manually close a position outside the bot.
        # We must: (1) run the full close path, (2) release capital, (3) reset DB cache.
        for phantom in phantoms:
            sym = phantom['symbol']
            logger.critical(
                f"👻 MANUAL CLOSE DETECTED: {sym} — broker is flat but internal "
                f"registry says open. Running full close path."
            )

            # Step 1: Use _finalize_closed_position if order_manager has this symbol.
            # This handles: active_positions cleanup + hard_stops cleanup +
            #               capital.release_slot() + db.log_trade_exit() atomically.
            finalized = False
            if self.order_manager and sym in self.order_manager.active_positions:
                try:
                    await self.order_manager._finalize_closed_position(
                        symbol=sym,
                        reason='MANUAL_CLOSE_DETECTED',
                        exit_price=0.0,
                        pnl=0.0,
                        send_alert=False,
                    )
                    finalized = True
                    logger.info(
                        f"[GHOST] _finalize_closed_position completed for {sym} — "
                        f"DB updated, capital released."
                    )
                except Exception as e:
                    logger.error(
                        f"[GHOST] _finalize_closed_position failed for {sym}: {e} — "
                        f"falling back to manual cleanup."
                    )

            if not finalized:
                # Fallback: manual cleanup if finalize failed or symbol wasn't in active_positions
                if self.order_manager:
                    self.order_manager.active_positions.pop(sym, None)
                    self.order_manager.hard_stops.pop(sym, None)
                    self.order_manager.exit_in_progress.pop(sym, None)

            # Step 2: Release capital slot if still occupied.
            # BUG FIX: Do NOT check active_symbol == sym.
            # This is a single-position bot — any phantom means slot should be free.
            if self.capital and not self.capital.is_slot_free:
                try:
                    await self.capital.release_slot(broker=self.broker)
                    logger.info(f"[GHOST] Capital slot force-released for phantom {sym}")
                except Exception as e:
                    logger.error(f"[GHOST] Capital force-release failed for {sym}: {e}")

            # Step 3: CRITICAL — mark DB dirty so next cycle re-fetches fresh positions.
            # Without this, _get_db_positions_cached() keeps returning stale cache
            # showing the position as OPEN → phantom detected every 6 seconds forever.
            self._db_dirty = True
            logger.info(f"[GHOST] _db_dirty set True for {sym} — DB will re-fetch next cycle.")

            # Step 4: Alert operator
            if self.telegram:
                await self.telegram.send_alert(
                    f"👻 *MANUAL CLOSE DETECTED*\n\n"
                    f"Symbol: `{sym}`\n"
                    f"You closed this position manually outside the bot.\n"
                    f"✅ Bot state synced.\n"
                    f"✅ Capital slot released.\n"
                    f"✅ DB position marked CLOSED.\n"
                    f"⚠️ PnL for this trade not tracked (manual exit)."
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
