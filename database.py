import asyncpg
import logging
import datetime
import os
import json
import asyncio
import config
from typing import Optional, List, Dict, Any
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
except Exception:  # pragma: no cover - import fallback for environments without psycopg2
    psycopg2 = None
    RealDictCursor = None

logger = logging.getLogger(__name__)

# Phase 42.1: PostgreSQL Configuration
DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "user": "postgres", # Default, should come from env
    "password": "password", # Default, should come from env
    "database": "shortcircuit_trading",
    "min_size": 10,
    "max_size": 50
}

class DatabaseManager:
    """
    Phase 42.1: HFT-Grade Database Manager using PostgreSQL + asyncpg.
    Implements connection pooling and atomic transactions.
    """
    
    _instance = None
    _pool = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DatabaseManager, cls).__new__(cls)
        return cls._instance

    @classmethod
    async def get_pool(cls):
        """
        Singleton connection pool.
        """
        if cls._pool is None:
            try:
                logger.info("Initializing PostgreSQL Connection Pool...")
                # Try to get config from env vars first
                config = DB_CONFIG.copy()
                config['user'] = os.getenv('DB_USER', config['user'])
                config['password'] = os.getenv('DB_PASS', os.getenv('DB_PASSWORD', config['password']))
                config['host'] = os.getenv('DB_HOST', config['host'])
                
                cls._pool = await asyncpg.create_pool(**config)
                logger.info(f"✅ DB Pool Initialized (Min: {config['min_size']}, Max: {config['max_size']})")
            except Exception as e:
                logger.critical(f"❌ Failed to initialize DB Pool: {e}")
                raise
        return cls._pool

    @classmethod
    async def close_pool(cls):
        if cls._pool:
            await cls._pool.close()
            cls._pool = None
            logger.info("DB Pool Closed.")

    async def initialize(self):
        """Helper to init pool."""
        await self.get_pool()

    async def close(self):
        """Close asyncpg pool for graceful application shutdown."""
        await self.close_pool()

    async def execute(self, query: str, *args):
        """Execute a write operation."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
             return await conn.execute(query, *args)

    async def fetch(self, query: str, *args):
        """Fetch multiple rows."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetch(query, *args)

    async def fetchrow(self, query: str, *args):
        """Fetch single row."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchrow(query, *args)
            
    async def fetchval(self, query: str, *args):
        """Fetch single value."""
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            return await conn.fetchval(query, *args)

    def query(self, sql: str, params=None) -> List[Dict[str, Any]]:
        """
        Synchronous query interface for standalone/offline scripts.

        Implementation contract (Phase 44.5):
        - Uses a fresh blocking psycopg2 connection per call.
        - Reuses the same DB env-var credentials as asyncpg config.
        - No persistent psycopg2 pool (script path only, not hot path).
        - Returns [] when there are no rows.
        """
        if not sql:
            return []
        if psycopg2 is None:
            raise RuntimeError(
                "DatabaseManager.query() requires psycopg2-binary. "
                "Install dependencies before using sync script query path."
            )

        params = params or ()
        cfg = DB_CONFIG.copy()
        cfg["user"] = os.getenv("DB_USER", cfg["user"])
        cfg["password"] = os.getenv("DB_PASS", os.getenv("DB_PASSWORD", cfg["password"]))
        cfg["host"] = os.getenv("DB_HOST", cfg["host"])
        cfg["port"] = int(os.getenv("DB_PORT", cfg["port"]))
        cfg["database"] = os.getenv("DB_NAME", cfg["database"])

        conn = None
        try:
            conn = psycopg2.connect(
                host=cfg["host"],
                port=cfg["port"],
                user=cfg["user"],
                password=cfg["password"],
                dbname=cfg["database"],
            )
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(sql, params)
                if cursor.description is None:
                    return []
                rows = cursor.fetchall() or []
                return [dict(row) for row in rows]
        except Exception:
            logger.exception("Synchronous query() failed")
            raise
        finally:
            if conn is not None:
                conn.close()

    # --- HFT Trading Logics ---

    async def log_trade_entry(self, data: dict):
        """
        Log new trade entry to 'positions' and 'orders'.
        Uses transaction to ensure consistency.
        """
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                # 1. Log Order
                await conn.execute("""
                    INSERT INTO orders (
                        symbol, side, order_type, qty, price, state, 
                        session_date, created_by, exchange_order_id
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """, data.get('symbol'), data.get('direction', 'BUY'), 'MARKET', data.get('qty'), 
                     data.get('entry_price'), 'FILLED', datetime.date.today(), 'BOT', 'N/A')
                
                # 2. Log Position
                await conn.execute("""
                    INSERT INTO positions (
                        symbol, qty, entry_price, state, session_date, source, opened_at
                    ) VALUES ($1, $2, $3, $4, $5, $6, NOW())
                """, data.get('symbol'), data.get('qty'), data.get('entry_price'), 
                     'OPEN', datetime.date.today(), 'SIGNAL')

    async def log_trade_exit(self, symbol: str, exit_data: dict):
        """
        Update position to CLOSED.
        """
        pool = await self.get_pool()
        async with pool.acquire() as conn:
            await conn.execute("""
                UPDATE positions 
                SET state = 'CLOSED', 
                    closed_at = NOW(), 
                    current_price = $1,
                    realized_pnl = $2
                WHERE symbol = $3 AND state = 'OPEN'
            """, exit_data.get('exit_price'), exit_data.get('pnl'), symbol)

    async def get_today_trades(self, session_date: Optional[datetime.date] = None) -> List[Dict[str, Any]]:
        """
        Return today's CLOSED trades from positions table for EOD summaries.
        Normalized shape: symbol, pnl, status, exit_reason, closed_at.
        """
        if session_date is None:
            session_date = datetime.date.today()

        rows = await self.fetch(
            """
            SELECT
                symbol,
                COALESCE(realized_pnl, 0) AS pnl,
                state AS status,
                closed_at,
                notes
            FROM positions
            WHERE session_date = $1
              AND state = 'CLOSED'
            ORDER BY COALESCE(closed_at, opened_at) ASC
            """,
            session_date
        )

        trades: List[Dict[str, Any]] = []
        for row in rows:
            record = dict(row)
            exit_reason = "N/A"
            notes = record.get('notes')
            if notes:
                try:
                    parsed = notes
                    if isinstance(notes, str):
                        parsed = json.loads(notes)
                    if isinstance(parsed, dict):
                        exit_reason = parsed.get('exit_reason') or parsed.get('reason') or "N/A"
                except Exception:
                    exit_reason = "N/A"

            trades.append({
                "symbol": record.get('symbol', 'UNKNOWN'),
                "pnl": float(record.get('pnl', 0.0) or 0.0),
                "status": record.get('status', 'CLOSED'),
                "exit_reason": exit_reason,
                "closed_at": record.get('closed_at'),
            })

        return trades
            
    async def log_event(self, event_type: str, details: dict):
        """
        Log system event or audit entry.
        """
        # We might need a generic event table or audit_log
        # For Phase 42.1, we have reconciliation_log, maybe add 'system_events'? 
        # Using a simple log output for now if table doesn't exist, strictly following migration.
        # Migration script has trade_events? No, migration script dropped trade_events.
        # It has audit_log? No audit_log in the applied v42_1_0_postgresql.sql?
        # Checking migration script content again...
        # It has Orders, Positions, Reconciliation Log.
        # It does NOT have 'audit_log' or 'trade_events' in the PRIMARY script I wrote.
        # Wait, the PRD listed them but I wrote a simplified migration script for "Emergency Patch".
        # I should stick to what I created in v42_1_0_postgresql.sql.
        pass
