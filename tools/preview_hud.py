import sys
import os
import asyncio
import time
import random
import threading

# Add project root to path so we can import dashboard modules
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dashboard_bridge import get_dashboard_bridge
from dashboard_server import start_dashboard_server

def run_preview():
    bridge = get_dashboard_bridge()
    print("🚀 AEGIS HUD PREVIEW MODE")
    print("👉 Open http://127.0.0.1:8555 in your browser")
    
    # Start server in thread
    srv_thread = threading.Thread(target=start_dashboard_server, kwargs={'port': 8555}, daemon=True)
    srv_thread.start()
    
    time.sleep(2) # Wait for server
    
    symbols = ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS"]
    gates = ["G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8", "G9", "G10", "G11", "G12", "G13"]
    
    try:
        while True:
            # 1. Heartbeat
            bridge.broadcast("HEARTBEAT", {
                "pnl": random.uniform(-500, 1500),
                "status": "SIMULATING"
            })
            
            # 2. Symbol Update
            sym = random.choice(symbols)
            bridge.broadcast("SYMBOL_UPDATE", {
                "symbol": sym,
                "ltp": random.uniform(1000, 3000),
                "gain_pct": random.uniform(5.5, 12.0),
                "rvol": random.uniform(1.2, 8.5),
                "slope": random.uniform(-5, 15),
                "nifty_trend": random.choice(["Bullish", "Bearish", "Neutral"])
            })
            
            # 3. Random Gate Updates
            for g in random.sample(gates, 3):
                status = random.choice(["PASS", "FAIL", "SCANNING"])
                bridge.broadcast("GATE_UPDATE", {"gate": g, "status": status})
            
            # 4. Neural Log
            if random.random() > 0.8:
                bridge.broadcast("SYSTEM_ALERT", {"msg": f"🎯 Simulated Discovery: {sym}"})
                
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\nStopping preview...")

if __name__ == "__main__":
    run_preview()
