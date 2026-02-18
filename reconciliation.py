
import asyncio
import logging
import json
from datetime import datetime, date
from database import DatabaseManager
from fyers_broker_interface import FyersBrokerInterface

logger = logging.getLogger(__name__)

class ReconciliationEngine:
    """
    Phase 42.2: HFT Reconciliation Engine with WebSocket Cache.
    
    Responsibilities:
    1. Continuous 2-second loop (Zero API cost due to cache).
    2. Compare DB State vs Broker Cache State.
    3. Detect Orphans (Broker has, DB doesn't).
    4. Detect Phantoms (DB has, Broker doesn't).
    5. Alert on Mismatches.
    """
    
    def __init__(self, broker: FyersBrokerInterface, db_manager: DatabaseManager, telegram_bot):
        self.broker = broker
        self.db = db_manager
        self.telegram = telegram_bot
        self.running = False
        self.interval = 2.0 # Default
        
    async def start(self):
        """Start the reconciliation loop."""
        if self.running: return
        self.running = True
        logger.info("✅ Reconciliation Engine Started (WebSocket Mode).")
        asyncio.create_task(self._loop())

    async def stop(self):
        self.running = False
        logger.info("🛑 Reconciliation Engine Stopped.")

    async def _loop(self):
        """
        Continuous reconciliation loop.

        Intervals:
        - MARKET HOURS (9:15 AM - 3:30 PM): every 6 seconds (tight)
        - PRE_MARKET / POST_MARKET with open positions: every 30 seconds
        - PRE_MARKET / POST_MARKET with NO positions: every 5 minutes
        """
        while self.running:
            start_time = asyncio.get_event_loop().time()

            try:
                await self.reconcile()
            except Exception as e:
                logger.error(f"Reconciliation error: {e}")

            elapsed_ms = (asyncio.get_event_loop().time() - start_time) * 1000

            # Determine next sleep interval based on session state
            interval = self._get_reconciliation_interval()

            # Only warn if slow during market hours (when it matters)
            # or if EXTREMELY slow (>3s) outside market hours
            if elapsed_ms > 500 and self._is_market_hours():
                logger.warning(f"⚠️ Slow Reconciliation: {elapsed_ms:.3f}ms")
            elif elapsed_ms > 3000:
                logger.warning(f"⚠️ Very Slow Reconciliation (off-hours): {elapsed_ms:.3f}ms")

            await asyncio.sleep(interval)

    def _get_reconciliation_interval(self) -> int:
        """
        Return reconciliation interval in seconds based on market state.
        """
        if self._is_market_hours():
            return 6  # Tight — every 6 seconds

        # Outside market hours
        # Check if we have any positions in DB or Broker (Approximate check)
        # We can't easily check "self._open_positions" because reconcile() is what fetches them.
        # But we can check if the last reconcile found any positions.
        # For safety/simplicity, if we are outside market hours, 300s is fine unless we know we have positions.
        # Let's assume 300s to stop the spam. 
        # If we really want to be safe, we could check DB quickly? 
        # But for now, following PRD logic strictly:
        # "POST/PRE with positions → 30s", "POST/PRE no positions → 300s"
        # We need to know if we have positions.
        # Let's query the broker interface cache directly if possible.
        
        has_positions = False
        try:
            # Quick check on broker cache (0 cost)
            if self.broker.position_cache:
                for p in self.broker.position_cache.values():
                    if p.net_qty != 0:
                        has_positions = True
                        break
        except: 
            pass
            
        return 30 if has_positions else 300

    def _is_market_hours(self) -> bool:
        """Check if currently within NSE trading hours."""
        from datetime import datetime, time as dtime
        import pytz

        try:
            IST = pytz.timezone('Asia/Kolkata')
        except:
            # Fallback if pytz missing or error, though it should be there.
            # Assuming server might be UTC, but let's try to be safe.
            # If datetime.now() is local time and machine is in IST:
            IST = None

        if IST:
            now = datetime.now(IST)
        else:
            now = datetime.now() # Fallback

        # Skip weekends
        if now.weekday() >= 5:  # Saturday=5, Sunday=6
            return False

        now_time = now.time()
        return dtime(9, 15) <= now_time <= dtime(15, 30)

    async def reconcile(self):
        """
        Perform one reconciliation pass using WebSocket Cache.
        """
        # 1. Get Broker State (From Cache - Fast)
        try:
            # This calls get_all_positions on broker which checks cache first, then REST
            cached_positions = await self.broker.get_all_positions()
            
            broker_positions = {
                p['symbol']: p for p in cached_positions
                # Filter out closed? get_all_positions usually returns open.
            }
        except Exception as e:
            logger.error(f"Reconciliation: Failed to fetch positions: {e}")
            return

        # 2. Get DB State
        try:
            db_rows = await self.db.fetch("SELECT symbol, qty FROM positions WHERE state = 'OPEN'")
            db_positions = {row['symbol']: row['qty'] for row in db_rows}
        except Exception as e:
            logger.error(f"Reconciliation: DB Read Failed: {e}")
            return

        # 3. Compare
        orphans = []
        phantoms = []
        mismatched = []

        # Check Broker vs DB (Orphans & Mismatches)
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

        # Check DB vs Broker (Phantoms)
        for symbol, db_qty in db_positions.items():
            if symbol not in broker_positions:
                phantoms.append({'symbol': symbol, 'qty': db_qty})

        # 4. Action / Log
        if orphans or phantoms or mismatched:
            logger.critical(f"🚨 DISCREPANCY: Orphans={len(orphans)}, Phantoms={len(phantoms)}, Mismatch={len(mismatched)}")
            
            # Log to DB
            try:
                log_entry = {
                    'internal_pos_count': len(db_positions),
                    'broker_pos_count': len(broker_positions),
                    'orphans_detected': json.dumps(orphans),
                    'phantoms_detected': json.dumps(phantoms),
                    'mismatches': json.dumps(mismatched),
                    'status': 'DIVERGENCE_DETECTED',
                    'session_date': date.today()
                }
                
                await self.db.execute("""
                    INSERT INTO reconciliation_log (
                        timestamp, internal_position_count, broker_position_count, 
                        orphaned_positions, phantom_positions, quantity_mismatches, 
                        status, session_date, check_duration_ms
                    ) VALUES (NOW(), $1, $2, $3, $4, $5, $6, $7, 0)
                """, log_entry['internal_pos_count'], log_entry['broker_pos_count'],
                     log_entry['orphans_detected'], log_entry['phantoms_detected'],
                     log_entry['mismatches'], log_entry['status'], log_entry['session_date'])
                
            except Exception as e:
                logger.error(f"Failed to log reconciliation discrepancy: {e}")

            # Alert
            if self.telegram:
                msg = "🚨 **RECONCILIATION ALERT**\n"
                if orphans: msg += f"Orphans: {orphans}\n"
                if phantoms: msg += f"Phantoms: {phantoms}\n"
                if mismatched: msg += f"Mismatch: {mismatched}\n"
                await self.telegram.send_alert(msg)
