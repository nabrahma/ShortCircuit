"""
Fetch today's signals from Telegram Bot chat history.
Uses getUpdates API to find messages sent by the bot.
"""
import os
import re
import json
import requests
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def get_bot_messages():
    """Fetch recent updates from Telegram Bot API."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"limit": 100, "offset": -100}
    
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        
        if not data.get("ok"):
            print(f"❌ API Error: {data}")
            return []
        
        return data.get("result", [])
    except Exception as e:
        print(f"❌ Request Error: {e}")
        return []

def parse_signal_from_text(text):
    """Extract signal data from GOD MODE message text."""
    signal = {}
    
    # Extract symbol (NSE:XXX-EQ)
    symbol_match = re.search(r"NSE:([A-Z0-9]+)-EQ", text)
    if symbol_match:
        signal["symbol"] = f"NSE:{symbol_match.group(1)}-EQ"
    
    # Extract price
    price_match = re.search(r"🏷️ Price:\s*([\d.]+)", text)
    if price_match:
        signal["entry"] = float(price_match.group(1))
    
    # Extract stop
    stop_match = re.search(r"🛑 Stop:\s*([\d.]+)", text)
    if stop_match:
        signal["stop"] = float(stop_match.group(1))
    
    # Extract qty
    qty_match = re.search(r"💰 Size:\s*(\d+)", text)
    if qty_match:
        signal["qty"] = int(qty_match.group(1))
    
    # Extract VWAP SD
    vwap_match = re.search(r"VWAP\s*\+?([\d.]+)SD", text)
    if vwap_match:
        signal["vwap_sd"] = float(vwap_match.group(1))
    
    # Extract pattern
    if "BEARISHENGULFING" in text:
        signal["pattern"] = "Bearish Engulfing"
    elif "SHOOTINGSTAR" in text or "SHOOTING_STAR" in text:
        signal["pattern"] = "Shooting Star"
    elif "EVENINGSTAR" in text or "EVENING_STAR" in text:
        signal["pattern"] = "Evening Star"
    else:
        signal["pattern"] = "Unknown"
    
    return signal if signal.get("symbol") else None

def main():
    print("📡 Fetching Telegram updates...")
    updates = get_bot_messages()
    
    if not updates:
        print("No updates found. Note: Bot messages may not appear in getUpdates.")
        print("Trying alternative approach - checking for callback queries...")
    
    today = datetime.now().strftime("%Y-%m-%d")
    signals_today = []
    
    for update in updates:
        # Check callback queries (button presses)
        if "callback_query" in update:
            cb = update["callback_query"]
            msg = cb.get("message", {})
            text = msg.get("text", "")
            
            if "GOD MODE" in text:
                timestamp = datetime.fromtimestamp(msg.get("date", 0))
                if timestamp.strftime("%Y-%m-%d") == today:
                    signal = parse_signal_from_text(text)
                    if signal:
                        signal["time"] = timestamp.strftime("%H:%M:%S")
                        signals_today.append(signal)
        
        # Check regular messages
        if "message" in update:
            msg = update["message"]
            text = msg.get("text", "")
            
            if "GOD MODE" in text:
                timestamp = datetime.fromtimestamp(msg.get("date", 0))
                if timestamp.strftime("%Y-%m-%d") == today:
                    signal = parse_signal_from_text(text)
                    if signal:
                        signal["time"] = timestamp.strftime("%H:%M:%S")
                        signals_today.append(signal)
    
    # Dedupe signals by symbol
    seen = set()
    unique_signals = []
    for s in signals_today:
        if s["symbol"] not in seen:
            seen.add(s["symbol"])
            unique_signals.append(s)
    
    print(f"\n📊 Found {len(unique_signals)} signals for {today}:\n")
    
    for i, sig in enumerate(unique_signals, 1):
        print(f"{i}. {sig.get('symbol', 'N/A')}")
        print(f"   Pattern: {sig.get('pattern', 'N/A')}")
        print(f"   Entry: {sig.get('entry', 'N/A')}")
        print(f"   Stop: {sig.get('stop', 'N/A')}")
        print(f"   Qty: {sig.get('qty', 'N/A')}")
        print(f"   VWAP: +{sig.get('vwap_sd', 'N/A')}SD")
        print(f"   Time: {sig.get('time', 'N/A')}")
        print()
    
    # Save to JSON for further analysis
    with open("todays_signals.json", "w") as f:
        json.dump(unique_signals, f, indent=2)
    
    print(f"💾 Saved to todays_signals.json")
    return unique_signals

if __name__ == "__main__":
    main()
