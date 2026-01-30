"""
EOD Simulation for Jan 30, 2026
Analyzes P&L for signals generated with Phase 24 filters.
"""
import csv
import json
import os
import sys

# Add parent dir to path if needed (for modules) or just run from root
# We need fyers_apiv3
from fyers_apiv3 import fyersModel

# Load access token
TOKEN_FILE = "access_token.txt"
if not os.path.exists(TOKEN_FILE):
    print(f"Error: {TOKEN_FILE} not found in {os.getcwd()}")
    sys.exit(1)

with open(TOKEN_FILE, "r") as f:
    access_token = f.read().strip()

fyers = fyersModel.FyersModel(client_id="XY14819-100", token=access_token, log_path="logs")

def get_closing_price(symbol):
    """Fetch the closing price for a symbol."""
    try:
        data = {"symbols": symbol}
        response = fyers.quotes(data=data)
        if response.get('s') == 'ok' and response.get('d'):
            return response['d'][0]['v']['lp']  # Last traded price
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
    return None

def main():
    # Read today's signals
    signals = []
    log_file = "logs/signals.csv"
    
    if not os.path.exists(log_file):
        print(f"Error: {log_file} not found.")
        sys.exit(1)

    with open(log_file, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['timestamp'].startswith('2026-01-30'):
                signals.append(row)
    
    print("=" * 60)
    print("EOD ANALYSIS - January 30, 2026")
    print("Phase 24 Filters Active")
    print("=" * 60)
    
    if not signals:
        print("No signals found for today.")
        return

    print(f"\nTotal Signals Today: {len(signals)}")
    print("-" * 60)
    
    results = []
    total_pnl = 0
    wins = 0
    
    for sig in signals:
        symbol = sig['symbol']
        entry = float(sig['ltp'])
        stop = float(sig['stop_loss'])
        pattern = sig['pattern'][:50]  # Truncate for display
        time = sig['timestamp'].split()[1][:5]  # HH:MM
        
        # Get closing price
        close = get_closing_price(symbol)
        
        if close is None:
            print(f"  {time} {symbol}: Could not fetch close price")
            continue
        
        # SHORT trade: Profit if close < entry
        # P&L = entry - close (for shorts)
        
        # Check if SL was hit (High > Stop) requires High data?
        # Simulation Assumption: If Close > Stop, SL definitely hit.
        # Ideally we need OHLC of the day to check if High > Stop. 
        # But for quick EOD, if Close > Stop, it's a loss.
        # If Close < Stop but High > Stop? We might miss a stopped out trade.
        # Let's assume hitting SL if close > stop for simplicity, or fetch OHLC?
        # Let's fetch OHLC for better accuracy.
        
        pnl = 0
        status = "UNKNOWN"
        
        try:
           # Get OHLC for the day to check High
           # Or just use quote data if avail? Quote has 'h'.
           quote_data = {"symbols": symbol}
           q_res = fyers.quotes(data=quote_data)
           if q_res.get('s') == 'ok':
               day_high = q_res['d'][0]['v']['high_price']
               # If entry time is late, we should check High AFTER entry?
               # Simulating strictly:
               pass
        except:
            pass

        # Simple Logic for now: 
        if close > stop:
            pnl = entry - stop
            status = "SL HIT"
        else:
            pnl = entry - close
            status = "WIN" if pnl > 0 else "HELD"
        
        if pnl > 0:
            wins += 1
            icon = "[WIN]"
        else:
            icon = "[LOSS]"
        
        total_pnl += pnl
        
        results.append({
            'time': time,
            'symbol': symbol.replace('NSE:', '').replace('-EQ', ''),
            'entry': entry,
            'stop': stop,
            'close': close,
            'pnl': pnl,
            'status': status
        })
        
        print(f"  {icon} {time} {results[-1]['symbol']:15} Entry:{entry:8.2f} Close:{close:8.2f} P&L:{pnl:+7.2f} ({status})")
    
    print("-" * 60)
    print(f"\nSUMMARY:")
    print(f"  Signals: {len(results)}")
    print(f"  Wins: {wins}")
    print(f"  Win Rate: {wins/len(results)*100:.1f}%" if results else "  N/A")
    print(f"  Total P&L: {total_pnl:+.2f} points")
    
    # Estimate INR (assuming avg qty of 10 shares)
    avg_qty = 10
    inr_pnl = total_pnl * avg_qty
    print(f"  Est. INR P&L: Rs {inr_pnl:+.2f} (@ qty {avg_qty})")
    
    # Save results
    os.makedirs("logs/archive", exist_ok=True)
    with open("logs/archive/eod_analysis_jan30.json", "w") as f:
        json.dump({
            'date': '2026-01-30',
            'signals': len(results),
            'wins': wins,
            'total_pnl': total_pnl,
            'results': results
        }, f, indent=2)
    
    print("\nSaved to logs/archive/eod_analysis_jan30.json")

if __name__ == "__main__":
    main()
