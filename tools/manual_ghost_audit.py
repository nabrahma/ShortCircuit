
import asyncio
import os
import sys
from datetime import date
from fyers_apiv3 import fyersModel

# Add project root to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from eod_analyzer import EODAnalyzer
from database import DatabaseManager
import config

async def manual_audit():
    print("🚀 Starting Manual Ghost Signal Audit...")
    
    # 1. Load Token
    token_path = "data/access_token.txt"
    if not os.path.exists(token_path):
        print("❌ Error: data/access_token.txt not found.")
        return
        
    with open(token_path, "r") as f:
        access_token = f.read().strip()
        
    # 2. Initialize Fyers
    client_id = config.FYERS_CLIENT_ID
    fyers = fyersModel.FyersModel(client_id=client_id, token=access_token, is_async=False, log_path="/tmp")
    
    # 3. Initialize DB
    db = DatabaseManager()
    
    # 4. Run Analyzer
    analyzer = EODAnalyzer(fyers_client=fyers, db_manager=db)
    
    target_date = date(2026, 4, 1)
    print(f"📅 Auditing trades for {target_date}...")
    
    report = await analyzer.run_daily_analysis(date=target_date)
    
    print("\n" + "="*50)
    print("🏆 GHOST SIGNAL AUDIT RESULTS 🏆")
    print("="*50)
    print(report)
    print("="*50)

if __name__ == "__main__":
    asyncio.run(manual_audit())
