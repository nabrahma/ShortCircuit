
import asyncio
import asyncpg
import json
import os
import sys
import datetime
from pathlib import Path

# Add current directory to path to import components
sys.path.append(os.getcwd())
from database import DB_CONFIG

_INSERT_SQL = """
    INSERT INTO gate_results (
        session_date, scan_id, evaluated_at, symbol,
        nifty_regime, nifty_level,
        g1_pass, g1_value, g2_pass, g2_value,
        g3_pass, g3_value, g4_pass, g4_value,
        g5_pass, g5_value, g6_pass, g6_value,
        g7_pass, g7_value, g8_pass, g8_value,
        g9_pass, g9_value, g10_pass, g10_value,
        g11_pass, g11_value, g12_pass, g12_value,
        verdict, first_fail_gate, rejection_reason,
        data_tier, entry_price, qty
    ) VALUES (
        $1, $2, $3, $4, $5, $6,
        $7, $8, $9, $10, $11, $12, $13, $14,
        $15, $16, $17, $18, $19, $20, $21, $22,
        $23, $24, $25, $26, $27, $28, $29, $30,
        $31, $32, $33, $34, $35, $36
    )
    ON CONFLICT DO NOTHING
"""

def _to_num(val):
    if val is None or val == 'None':
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None

def _to_bool(val):
    if val is None or val == 'None':
        return None
    if isinstance(val, bool):
        return val
    return str(val).lower() == 'true'

def _to_dt(val):
    if val is None or val == 'None':
        return None
    try:
        return datetime.datetime.fromisoformat(val)
    except (TypeError, ValueError):
        return None

async def reimport_file(file_path, conn):
    print(f"Reading {file_path}...")
    rows = []
    count = 0
    with open(file_path, 'r', encoding='utf-8') as f:
        for line in f:
            if not line.strip():
                continue
            data = json.loads(line)
            
            # Map JSON keys to DB columns (positional)
            # GateResult __dict__ mapping
            row = (
                datetime.date.fromisoformat(data['evaluated_at'][:10]) if 'evaluated_at' in data else datetime.date.today(),
                int(data.get('scan_id', 0)),
                _to_dt(data.get('evaluated_at')),
                data.get('symbol'),
                data.get('nifty_regime'),
                _to_num(data.get('nifty_level')),
                # Gates
                _to_bool(data.get('g1_pass')), _to_num(data.get('g1_value')),
                _to_bool(data.get('g2_pass')), _to_num(data.get('g2_value')),
                _to_bool(data.get('g3_pass')), _to_num(data.get('g3_value')),
                _to_bool(data.get('g4_pass')), _to_num(data.get('g4_value')),
                _to_bool(data.get('g5_pass')), _to_num(data.get('g5_value')),
                _to_bool(data.get('g6_pass')), str(data.get('g6_value')) if data.get('g6_value') is not None else None,
                _to_bool(data.get('g7_pass')), str(data.get('g7_value')) if data.get('g7_value') is not None else None,
                _to_bool(data.get('g8_pass')), _to_num(data.get('g8_value')),
                _to_bool(data.get('g9_pass')), str(data.get('g9_value')) if data.get('g9_value') is not None else None,
                _to_bool(data.get('g10_pass')), _to_num(data.get('g10_value')),
                _to_bool(data.get('g11_pass')), _to_num(data.get('g11_value')),
                _to_bool(data.get('g12_pass')), _to_num(data.get('g12_value')),
                # Outcome
                data.get('verdict', 'PENDING'),
                data.get('first_fail_gate'),
                data.get('rejection_reason'),
                data.get('data_tier'),
                _to_num(data.get('entry_price')),
                int(data['qty']) if data.get('qty') is not None and data.get('qty') != 'None' else None
            )
            rows.append(row)
            count += 1
            
            if len(rows) >= 500:
                await conn.executemany(_INSERT_SQL, rows)
                rows = []

    if rows:
        await conn.executemany(_INSERT_SQL, rows)
    
    print(f"Successfully re-imported {count} records from {file_path}")
    return count

async def main():
    # Load .env
    env_path = ".env"
    if os.path.exists(env_path):
        with open(env_path, 'r') as f:
            for line in f:
                if '=' in line:
                    key, value = line.strip().split('=', 1)
                    os.environ[key] = value

    user = os.getenv('DB_USER', DB_CONFIG['user'])
    password = os.getenv('DB_PASSWORD', DB_CONFIG['password'])
    host = os.getenv('DB_HOST', DB_CONFIG['host'])
    database = os.getenv('DB_NAME', DB_CONFIG['database'])
    port = os.getenv('DB_PORT', '5432')

    try:
        conn = await asyncpg.connect(
            user=user,
            password=password,
            host=host,
            port=port,
            database=database
        )
    except Exception as e:
        print(f"Database connection failed: {e}")
        return

    try:
        # Find all gate_fallback_*.jsonl files in logs/
        log_dir = Path("logs")
        fallback_files = list(log_dir.glob("gate_fallback_*.jsonl"))
        
        if not fallback_files:
            print("No fallback files found in logs/ directory.")
            return

        print(f"Found {len(fallback_files)} fallback files. Starting re-import...")
        total_imported = 0
        for f in sorted(fallback_files):
            total_imported += await reimport_file(str(f), conn)
            
            # Archive the file
            archive_path = f.with_suffix('.jsonl.imported')
            try:
                os.rename(str(f), str(archive_path))
                print(f"Archived {f.name} to {archive_path.name}")
            except Exception as e:
                print(f"Failed to archive {f.name}: {e}")

        print(f"Total records re-imported: {total_imported}")

    finally:
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
