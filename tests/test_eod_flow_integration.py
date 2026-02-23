import asyncio
import os
from datetime import date
from uuid import uuid4

import asyncpg
import pytest
from dotenv import load_dotenv

from eod_analyzer import EODAnalyzer

pytestmark = pytest.mark.integration


def _run(coro):
    return asyncio.run(coro)


def _db_config():
    return {
        "user": os.getenv("DB_USER", "postgres"),
        "password": os.getenv("DB_PASSWORD", "password"),
        "database": "shortcircuit_trading",
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", "5432")),
    }


async def _db_available() -> bool:
    conn = None
    try:
        conn = await asyncpg.connect(**_db_config())
        has_positions = await conn.fetchval(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name = 'positions'
            )
            """
        )
        return bool(has_positions)
    except Exception:
        return False
    finally:
        if conn:
            await conn.close()


async def _insert_and_fetch_trade(symbol: str, session_date: date):
    conn = await asyncpg.connect(**_db_config())
    try:
        await conn.execute(
            """
            INSERT INTO positions (symbol, qty, entry_price, realized_pnl, state, source, session_date, notes)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            symbol,
            -10,
            100.0,
            120.0,
            "CLOSED",
            "SIGNAL",
            session_date,
            "eod integration test row",
        )
        rows = await conn.fetch(
            """
            SELECT symbol, entry_price, COALESCE(realized_pnl, 0) AS realized_pnl, state
            FROM positions
            WHERE symbol = $1 AND session_date = $2
            """,
            symbol,
            session_date,
        )
        return rows
    finally:
        await conn.close()


async def _cleanup_trade(symbol: str, session_date: date):
    conn = await asyncpg.connect(**_db_config())
    try:
        await conn.execute(
            "DELETE FROM positions WHERE symbol = $1 AND session_date = $2",
            symbol,
            session_date,
        )
    finally:
        await conn.close()


class PostgresEODAdapter:
    """
    Adapter that feeds EODAnalyzer with rows fetched from real PostgreSQL.
    """

    def __init__(self, trades):
        self._trades = trades
        self.logged_events = []

    def query(self, query: str, params):
        if "FROM trades" in query:
            return self._trades
        if "FROM soft_stop_events" in query:
            return []
        return []

    def log_event(self, event_type: str, details: dict):
        self.logged_events.append((event_type, details))


def test_eod_pipeline_postgres_integration():
    """
    Integration coverage: uses real PostgreSQL reads/writes for EOD input data.
    Skips automatically when DB is unavailable.
    """
    load_dotenv(".env")

    if not _run(_db_available()):
        pytest.skip("PostgreSQL unavailable; skipping integration test.")

    session_date = date(2099, 1, 3)
    symbol = f"NSE:EODINT{uuid4().hex[:6]}-EQ"

    rows = _run(_insert_and_fetch_trade(symbol, session_date))
    trades = []
    for row in rows:
        entry_price = float(row["entry_price"] or 0.0)
        pnl = float(row["realized_pnl"] or 0.0)
        pnl_pct = (pnl / entry_price * 100.0) if entry_price else None
        trades.append(
            {
                "symbol": row["symbol"],
                "entry_price": entry_price,
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "status": row["state"],
            }
        )

    adapter = PostgresEODAdapter(trades)
    try:
        analyzer = EODAnalyzer(None, adapter)
        report = analyzer.run_daily_analysis(session_date.isoformat())

        assert "# 📊 EOD Report" in report
        assert "No Trades Executed Today." not in report
        assert any(event[0] == "daily_summaries" for event in adapter.logged_events)
    finally:
        _run(_cleanup_trade(symbol, session_date))
