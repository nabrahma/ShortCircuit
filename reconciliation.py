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

    def __init__(self, broker: FyersBrokerInterface, db_manager: DatabaseManager, telegram_bot):
        self.broker = broker
        self.db = db_manager
        self.telegram = telegram_bot
        self.running = False

        # ── Internal State Cache ──────────────────────────────────────
        self._db_positions: dict = {}        # symbol → qty (last known DB state)
        self._db_dirty: bool = True          # True = re-fetch DB next cycle
        self._has_open_positions: bool = False  # master flat/live flag
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
        self.running = True
        logger.info("✅ Reconciliation Engine Started (WebSocket Mode).")
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        logger.info("🛑 Reconciliation Engine Stopped.")

    async def _loop(self):
        while self.running:
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

            await asyncio.sleep(interval)

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
                    result[symbol] = {'qty': p.net_qty, 'symbol': symbol}
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

    async def _handle_divergence(self, db_pos, broker_pos, orphans, phantoms, mismatched):
        """Log + alert on any state divergence."""
        logger.critical(
            f"🚨 DISCREPANCY: Orphans={len(orphans)}, "
            f"Phantoms={len(phantoms)}, Mismatch={len(mismatched)}"
        )

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

        if self.telegram:
            msg = "🚨 **RECONCILIATION ALERT**\n"
            if orphans:    msg += f"Orphans: {orphans}\n"
            if phantoms:   msg += f"Phantoms: {phantoms}\n"
            if mismatched: msg += f"Mismatch: {mismatched}\n"
            await self.telegram.send_alert(msg)

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
