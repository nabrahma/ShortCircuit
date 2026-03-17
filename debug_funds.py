import asyncio
import os
import json
from fyers_connect import FyersConnect
from fyers_broker_interface import FyersBrokerInterface

async def dump_funds():
    print("Initializing Fyers Connection...")
    client = FyersConnect().authenticate()
    broker = FyersBrokerInterface(client)
    
    print("Fetching Funds...")
    funds = await broker.get_funds()
    
    print("\n--- FYERS FUNDS RESPONSE ---")
    print(json.dumps(funds, indent=2))
    print("----------------------------\n")
    
    # Simulate CapitalManager parsing
    print("--- PARSING TEST ---")
    for item in funds.get('fund_limit', []):
        print(f"ID: {item.get('id')} | Title: {item.get('title')} | Equity: {item.get('equityAmount')}")
        if item.get('id') == 2:
            print(f"  >>> MATCHED ID 2: {item.get('equityAmount')}")
        if 'available' in str(item.get('title', '')).lower():
            print(f"  >>> MATCHED 'available': {item.get('equityAmount')}")

if __name__ == "__main__":
    asyncio.run(dump_funds())
