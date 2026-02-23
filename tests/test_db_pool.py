
import asyncio
import pytest
import logging
from database import DatabaseManager
import time
from dotenv import load_dotenv

# Ensure DB_* env vars are loaded even when this test is run in isolation.
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_connection_pooling():
    """
    Verify that the connection pool initializes and handles concurrent requests.
    """
    db = DatabaseManager()
    pool = await db.get_pool()
    
    assert pool is not None
    assert len(pool._holders) >= 10  # Min size check (internal asyncpg implementation detail)
    
    logger.info("✅ Connection Pool Initialized")

    table_name = "test_concurrency"

    # Prepare table once (DDL outside concurrent workers to avoid race on pg_type)
    async with pool.acquire() as conn:
        await conn.execute(f"CREATE TABLE IF NOT EXISTS {table_name} (id INT, val TEXT)")
        await conn.execute(f"TRUNCATE TABLE {table_name}")

    # Test concurrent writes
    async def write_op(i):
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {table_name} (id, val) VALUES ($1, $2)",
                i,
                f"test_{i}"
            )
            return i

    start_time = time.time()
    tasks = [write_op(i) for i in range(20)]
    results = await asyncio.gather(*tasks)
    end_time = time.time()
    
    duration = end_time - start_time
    logger.info(f"✅ 20 Concurrent Writes completed in {duration:.4f}s")
    
    assert len(results) == 20
    assert duration < 1.0, "Concurrent writes too slow!"

    # Clean up
    async with pool.acquire() as conn:
        await conn.execute(f"DROP TABLE IF EXISTS {table_name}")
    
    await db.close_pool()
