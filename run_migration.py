import asyncio
import os
import asyncpg
from dotenv import load_dotenv

async def run_migration():
    # Load environment variables from .env
    load_dotenv()
    
    print("Connecting to database...")
    user = os.getenv('DB_USER', 'postgres')
    password = os.getenv('DB_PASSWORD', 'password')
    host = os.getenv('DB_HOST', 'localhost')
    port = os.getenv('DB_PORT', '5432')
    database = os.getenv('DB_NAME', 'shortcircuit_trading')
    
    conn = await asyncpg.connect(user=user, password=password, host=host, port=port, database=database)
    
    migration_file = r"d:\For coding\ShortCircuit\migrations\v56_schema_expansion.sql"
    print(f"Reading migration file: {migration_file}")
    
    with open(migration_file, 'r') as f:
        sql = f.read()
    
    print("Executing migration...")
    try:
        await conn.execute(sql)
        print("✅ Migration successful!")
    except Exception as e:
        print(f"❌ Migration failed: {e}")
    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(run_migration())
