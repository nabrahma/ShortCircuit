
import asyncio
import os
from dotenv import load_dotenv
load_dotenv()
from database import DatabaseManager

async def fetch_rejections():
    db = DatabaseManager()
    try:
        pool = await db.get_pool()
        print("Querying for March 17th rejections...")
        query = """
            SELECT symbol, first_fail_gate, rejection_reason 
            FROM gate_results 
            WHERE session_date = '2026-03-17' 
              AND verdict = 'REJECTED'
            ORDER BY evaluated_at ASC
        """
        rows = await db.fetch(query)
        print(f"Total rejections found: {len(rows)}")
        
        # Group by gate
        gate_counts = {}
        for r in rows:
            gate = r['first_fail_gate']
            gate_counts[gate] = gate_counts.get(gate, 0) + 1
        
        print("\nRejection Breakdown by Gate:")
        for gate, count in sorted(gate_counts.items(), key=lambda x: x[1], reverse=True):
            print(f"  {gate}: {count}")

        print("\nSample G7_REGIME Rejections (Climax Window):")
        g7_rows = [r for r in rows if r['first_fail_gate'] == 'G7_REGIME']
        for r in g7_rows[:20]:
            print(f"  {r['symbol']} | Reason: {r['rejection_reason']}")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await db.close()

if __name__ == "__main__":
    asyncio.run(fetch_rejections())
