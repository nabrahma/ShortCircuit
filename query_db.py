import asyncio
import asyncpg

async def main():
    conn = await asyncpg.connect(
        user='botuser', password='trading_pass_123',
        database='shortcircuit_trading', host='localhost'
    )
    rows = await conn.fetch("""
        SELECT rejection_reason, COUNT(*) as c 
        FROM gate_results 
        WHERE session_date = CURRENT_DATE 
          AND first_fail_gate = 'G1_GAIN_CONSTRAINTS' 
        GROUP BY rejection_reason 
        ORDER BY c DESC LIMIT 10;
    """)
    for r in rows:
        print(f"{r['c']} - {r['rejection_reason']}")
    await conn.close()

asyncio.run(main())
