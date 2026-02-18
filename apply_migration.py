
import asyncio
import asyncpg
import os

def load_env():
    env_path = "d:/For coding/ShortCircuit/.env"
    if os.path.exists(env_path):
        with open(env_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"): continue
                if "=" in line:
                    key, val = line.split("=", 1)
                    os.environ[key] = val

async def apply_migration():
    load_env()
    print("🚀 Applying Migration: v42_1_0_postgresql.sql")
    try:
        # Connect to DB
        conn = await asyncpg.connect(
            user=os.environ.get("DB_USER", "postgres"),
            password=os.environ.get("DB_PASSWORD", "password"),
            database=os.environ.get("DB_NAME", "shortcircuit_trading"),
            host=os.environ.get("DB_HOST", "localhost"),
            port=os.environ.get("DB_PORT", 5432)
        )
        print("✅ Connected to Database")
        
        # Read SQL
        with open("d:/For coding/ShortCircuit/migrations/v42_1_0_postgresql.sql", "r") as f:
            sql = f.read()
            
        # Execute
        await conn.execute(sql)
        print("✅ Migration Applied Successfully!")
        await conn.close()
        
    except Exception as e:
        print(f"❌ Migration Failed: {e}")

if __name__ == "__main__":
    asyncio.run(apply_migration())
