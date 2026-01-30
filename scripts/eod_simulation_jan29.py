"""
EOD Simulation for Jan 29, 2026
Analyzes P&L for signals generated with Phase 24 filters.
"""
import csv
from fyers_apiv3 import fyersModel
import json

# Load access token
with open("access_token.txt", "r") as f:
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
    with open("logs/signals.csv", "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['timestamp'].startswith('2026-01-29'):
                signals.append(row)
    
    print("=" * 60)
    print("EOD ANALYSIS - January 29, 2026")
    print("Phase 24 Filters Active (First Day)")
    print("=" * 60)
    
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
        # But check if SL was hit: if close > stop, loss = stop - entry
        
        if close > stop:
            # SL hit
            pnl = entry - stop  # Negative (loss)
            status = "SL HIT"
        else:
            # Held to close
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
    print(f"  Losses: {len(results) - wins}")
    print(f"  Win Rate: {wins/len(results)*100:.1f}%" if results else "  N/A")
    print(f"  Total P&L: {total_pnl:+.2f} points")
    
    # Estimate INR (assuming avg qty of 10 shares)
    avg_qty = 10
    inr_pnl = total_pnl * avg_qty
    print(f"  Est. INR P&L: Rs {inr_pnl:+.2f} (@ qty {avg_qty})")
    
    print("\n" + "=" * 60)
    print("COMPARISON: Yesterday vs Today")
    print("=" * 60)
    print("  Yesterday (No Filters): 81 signals, 35% win rate, -13.86 pts")
    print(f"  Today (Phase 24):       {len(results)} signals, {wins/len(results)*100:.1f}% win rate, {total_pnl:+.2f} pts" if results else "")
    print("=" * 60)
    
    # Save results
    with open("eod_analysis_jan29.json", "w") as f:
        json.dump({
            'date': '2026-01-29',
            'signals': len(results),
            'wins': wins,
            'total_pnl': total_pnl,
            'results': results
        }, f, indent=2)
    
    print("\nSaved to eod_analysis_jan29.json")

if __name__ == "__main__":
    main()
