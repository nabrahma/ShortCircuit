"""
EOD Analysis Script for Jan 28, 2026
Simulates all 86 signals to calculate hypothetical P&L
"""
import json
from datetime import datetime
from fyers_connect import FyersConnect
from collections import defaultdict

# Parse the signals from user input
SIGNALS_RAW = """
NSE:ASIANENE-EQ,254.6,255.29821428571427,10:40
NSE:STLTECH-EQ,93.52,94.02071428571429,10:42
NSE:MMTC-EQ,69.29,69.72142857142858,11:00
NSE:ASIANENE-EQ,257.25,259.41785714285714,11:11
NSE:HINDCOPPER-EQ,613.25,616.825,11:11
NSE:STLTECH-EQ,94.8,95.36,11:18
NSE:MMTC-EQ,69.36,70.01,11:22
NSE:OIL-EQ,489.95,491.375,11:24
NSE:ASIANENE-EQ,258,259.6928571428571,11:30
NSE:AVANTEL-EQ,137.66,137.92,11:54
NSE:CRIZAC-EQ,259.8,261.07142857142856,12:01
NSE:ASIANENE-EQ,256.1,257.26964285714286,12:10
NSE:REDTAPE-EQ,123,123.85,12:14
NSE:REDTAPE-EQ,121.75,123.0,12:15
NSE:INFOBEAN-EQ,845.9,850.2375,12:16
NSE:AVANTEL-EQ,139.88,140.40785714285715,12:21
NSE:AVANTEL-EQ,139.84,140.35785714285714,12:24
NSE:AVANTEL-EQ,139.07,139.64392857142857,12:31
NSE:TEJASNET-EQ,335.7,337.6267857142857,12:43
NSE:DATAPATTNS-EQ,2551,2557.9392857142857,12:45
NSE:TEJASNET-EQ,335.05,337.21785714285716,12:46
NSE:DATAPATTNS-EQ,2541.8,2554.1499999999996,12:46
NSE:REDTAPE-EQ,123.79,124.35,12:52
NSE:RPEL-EQ,737.25,738.6321428571429,12:54
NSE:TEJASNET-EQ,336.95,342.7517857142857,12:57
NSE:ASIANENE-EQ,257.05,258.35,12:57
NSE:TEJASNET-EQ,334.35,338.1089285714286,13:00
NSE:RPEL-EQ,741.9,743.5285714285715,13:05
NSE:HINDCOPPER-EQ,629.5,631.1357142857142,13:33
NSE:AVANTEL-EQ,143.23,143.8857142857143,13:36
NSE:REDTAPE-EQ,126.91,127.51392857142858,13:37
NSE:AVANTEL-EQ,142.64,143.7942857142857,13:37
NSE:RTNINDIA-EQ,36.07,36.64,13:38
NSE:HINDCOPPER-EQ,630.05,631.0357142857143,13:41
NSE:DATAPATTNS-EQ,2550,2553.95,13:44
NSE:TEJASNET-EQ,335.35,336.96964285714284,13:45
NSE:DATAPATTNS-EQ,2544,2549.210714285714,13:47
NSE:HINDCOPPER-EQ,630.45,631.2892857142857,13:51
NSE:TEJASNET-EQ,338.25,339.95000000000005,13:56
NSE:PRECWIRE-EQ,250.83,251.28892857142856,14:00
NSE:PRECWIRE-EQ,247.6,249.9942857142857,14:06
NSE:MAHLOG-EQ,343.4,344.76428571428573,14:07
NSE:OIL-EQ,492.8,493.9428571428571,14:09
NSE:SHILCTECH-EQ,3354,3367.4928571428572,14:14
NSE:REDTAPE-EQ,125.49,126.92,14:20
NSE:MAHLOG-EQ,350.9,352.93035714285713,14:27
NSE:AVANTEL-EQ,144.78,145.05,14:31
NSE:TEJASNET-EQ,335.8,337.19464285714287,14:31
NSE:AVANTEL-EQ,145.26,145.66857142857143,14:34
NSE:PRECWIRE-EQ,248.62,249.1,14:34
NSE:AVANTEL-EQ,145.35,146.25464285714287,14:36
NSE:AVANTEL-EQ,143.39,143.8125,14:46
NSE:WANBURY-EQ,185.43,187.24678571428572,14:47
NSE:HINDCOPPER-EQ,635.8,636.7267857142857,14:49
NSE:DATAPATTNS-EQ,2603.3,2615.3785714285714,14:51
NSE:DATAPATTNS-EQ,2609.4,2614.5035714285714,14:57
NSE:TEJASNET-EQ,340.5,342.6714285714286,14:59
NSE:WANBURY-EQ,187.8,188.60821428571427,15:01
NSE:TEJASNET-EQ,340.25,342.83035714285717,15:01
NSE:DATAPATTNS-EQ,2620.9,2629.4035714285715,15:03
NSE:TEJASNET-EQ,336.9,340.7339285714286,15:03
NSE:HINDCOPPER-EQ,628.75,634.6660714285714,15:11
NSE:TEJASNET-EQ,339.2,343.3267857142857,15:13
NSE:DATAPATTNS-EQ,2611.2,2626.203571428571,15:13
NSE:AVANTEL-EQ,144,144.73,15:13
NSE:TEJASNET-EQ,336.95,341.76964285714286,15:14
NSE:OLECTRA-EQ,1088.8,1093.0714285714287,15:14
NSE:SCHNEIDER-EQ,697.85,699.3678571428571,15:14
NSE:DATAPATTNS-EQ,2612.1,2621.1392857142855,15:15
NSE:AVANTEL-EQ,143.25,143.93,15:15
NSE:DATAPATTNS-EQ,2608.5,2619.8,15:16
NSE:AVANTEL-EQ,143.16,143.55,15:18
NSE:PRECWIRE-EQ,247.9,248.62,15:19
NSE:SPENCERS-EQ,34.39,34.64,15:20
NSE:BEML-EQ,1819.5,1824.992857142857,15:20
NSE:HINDCOPPER-EQ,636.65,640.25,15:23
NSE:SPENCERS-EQ,34.15,34.53,15:23
NSE:SCHNEIDER-EQ,701.6,705.4821428571429,15:25
NSE:DATAPATTNS-EQ,2618.1,2657.046428571429,15:26
NSE:HINDCOPPER-EQ,637.05,640.3125,15:27
NSE:OLECTRA-EQ,1087.6,1091.2607142857144,15:28
"""

def parse_signals():
    signals = []
    for line in SIGNALS_RAW.strip().split('\n'):
        if not line.strip():
            continue
        parts = line.split(',')
        if len(parts) >= 4:
            signals.append({
                'symbol': parts[0],
                'entry': float(parts[1]),
                'stop': float(parts[2]),
                'time': parts[3]
            })
    return signals

def get_closing_price(fyers, symbol):
    """Get the closing price for today"""
    import datetime
    today = datetime.date.today().strftime("%Y-%m-%d")
    
    data = {
        "symbol": symbol,
        "resolution": "1",
        "date_format": "1",
        "range_from": today,
        "range_to": today,
        "cont_flag": "1"
    }
    
    try:
        response = fyers.history(data=data)
        if response.get('s') == 'ok' and response.get('candles'):
            # Get the last candle's close
            return response['candles'][-1][4]
    except Exception as e:
        print(f"Error fetching {symbol}: {e}")
    
    return None

def simulate_trade(entry, stop, close_price):
    """
    Simulate a SHORT trade.
    Returns P&L in points.
    """
    risk = stop - entry  # This is the risk per share
    
    # Check if stopped out (price went above stop)
    if close_price >= stop:
        return -risk, "SL Hit"
    else:
        # Profit = entry - close (for short)
        return entry - close_price, "Held"

def main():
    print("=" * 60)
    print("📊 EOD ANALYSIS: January 28, 2026")
    print("=" * 60)
    
    signals = parse_signals()
    print(f"\n📈 Total Signals: {len(signals)}")
    
    # Count by symbol
    symbol_counts = defaultdict(int)
    for s in signals:
        symbol_counts[s['symbol']] += 1
    
    print("\n🔄 Signal Frequency by Symbol:")
    for sym, count in sorted(symbol_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"   {sym}: {count} signals")
    
    # Initialize Fyers
    print("\n🔌 Connecting to Fyers...")
    fyers_conn = FyersConnect()
    fyers = fyers_conn.authenticate()
    
    # Get unique symbols
    unique_symbols = list(symbol_counts.keys())
    
    # Fetch closing prices
    print(f"\n📡 Fetching closing prices for {len(unique_symbols)} symbols...")
    closing_prices = {}
    for sym in unique_symbols:
        price = get_closing_price(fyers, sym)
        if price:
            closing_prices[sym] = price
            print(f"   {sym}: {price}")
    
    # Simulate each trade
    print("\n💰 Simulating Trades...")
    results = []
    total_pnl = 0
    wins = 0
    losses = 0
    
    for sig in signals:
        symbol = sig['symbol']
        if symbol not in closing_prices:
            continue
        
        close = closing_prices[symbol]
        pnl, status = simulate_trade(sig['entry'], sig['stop'], close)
        
        results.append({
            'symbol': symbol,
            'entry': sig['entry'],
            'stop': sig['stop'],
            'close': close,
            'pnl': pnl,
            'status': status
        })
        
        total_pnl += pnl
        if pnl > 0:
            wins += 1
        else:
            losses += 1
    
    # Summary
    print("\n" + "=" * 60)
    print("📊 RESULTS SUMMARY")
    print("=" * 60)
    print(f"Total Trades Simulated: {len(results)}")
    print(f"Wins: {wins}")
    print(f"Losses: {losses}")
    print(f"Win Rate: {(wins/len(results)*100) if results else 0:.1f}%")
    print(f"\nTotal P&L (Points): {total_pnl:.2f}")
    
    # Estimate INR (rough, based on avg qty ~10)
    avg_qty = 10
    estimated_inr = total_pnl * avg_qty
    print(f"Estimated INR P&L (@ avg 10 qty): ₹{estimated_inr:.2f}")
    
    # Show worst trades
    print("\n🔴 Worst Trades:")
    sorted_results = sorted(results, key=lambda x: x['pnl'])
    for r in sorted_results[:5]:
        print(f"   {r['symbol']}: {r['pnl']:.2f} pts ({r['status']})")
    
    # Show best trades
    print("\n🟢 Best Trades:")
    for r in sorted_results[-5:]:
        print(f"   {r['symbol']}: {r['pnl']:.2f} pts ({r['status']})")
    
    # Save results
    with open("eod_analysis_jan28.json", "w") as f:
        json.dump({
            'total_signals': len(signals),
            'unique_symbols': len(unique_symbols),
            'symbol_frequency': dict(symbol_counts),
            'results': results,
            'summary': {
                'wins': wins,
                'losses': losses,
                'total_pnl': total_pnl,
                'win_rate': (wins/len(results)*100) if results else 0
            }
        }, f, indent=2)
    
    print("\n💾 Saved to eod_analysis_jan28.json")
    
    return results

if __name__ == "__main__":
    main()
