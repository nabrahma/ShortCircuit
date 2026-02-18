
import asyncio
import pytest
import logging
from database import DatabaseManager
import time

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

    # Test Concurrent Writes
    async def write_op(i):
        async with pool.acquire() as conn:
            # Create a dummy table if not exists
            await conn.execute("CREATE TABLE IF NOT EXISTS test_concurrency (id INT, val TEXT)")
            await conn.execute("INSERT INTO test_concurrency (id, val) VALUES ($1, $2)", i, f"test_{i}")
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
        await conn.execute("DROP TABLE IF EXISTS test_concurrency")
    
    await db.close_pool()
