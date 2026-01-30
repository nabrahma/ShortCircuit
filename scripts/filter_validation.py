"""
Filter Validation: What signals would pass with stricter criteria?
ASCII output version for Windows compatibility.
"""

SIGNALS = [
    {"time": "10:40", "symbol": "ASIANENE", "why": "Absorption", "vwap_sd": None, "pnl": -0.70},
    {"time": "10:42", "symbol": "STLTECH", "why": "Absorption", "vwap_sd": None, "pnl": -0.50},
    {"time": "11:00", "symbol": "MMTC", "why": "Absorption", "vwap_sd": None, "pnl": 1.64},
    {"time": "11:11", "symbol": "ASIANENE", "why": "Absorption", "vwap_sd": None, "pnl": -1.65},
    {"time": "11:11", "symbol": "HINDCOPPER", "why": "Absorption", "vwap_sd": None, "pnl": -3.58},
    {"time": "11:18", "symbol": "STLTECH", "why": "Absorption", "vwap_sd": None, "pnl": -0.56},
    {"time": "11:22", "symbol": "MMTC", "why": "Absorption", "vwap_sd": None, "pnl": 1.71},
    {"time": "11:24", "symbol": "OIL", "why": "Absorption", "vwap_sd": None, "pnl": -1.43},
    {"time": "11:30", "symbol": "ASIANENE", "why": "Absorption", "vwap_sd": None, "pnl": -0.90},
    {"time": "11:54", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.26},
    {"time": "12:01", "symbol": "CRIZAC", "why": "Absorption", "vwap_sd": None, "pnl": 8.85},
    {"time": "12:10", "symbol": "ASIANENE", "why": "TAPESTALL+VWAP", "vwap_sd": 4.2, "pnl": -1.17},
    {"time": "12:14", "symbol": "REDTAPE", "why": "Absorption", "vwap_sd": None, "pnl": -0.85},
    {"time": "12:15", "symbol": "REDTAPE", "why": "Absorption", "vwap_sd": None, "pnl": -1.25},
    {"time": "12:16", "symbol": "INFOBEAN", "why": "Absorption", "vwap_sd": None, "pnl": -4.34},
    {"time": "12:21", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.53},
    {"time": "12:24", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.52},
    {"time": "12:31", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.57},
    {"time": "12:43", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -1.93},
    {"time": "12:45", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": -6.94},
    {"time": "12:46", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -2.17},
    {"time": "12:46", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": -12.35},
    {"time": "12:52", "symbol": "REDTAPE", "why": "Absorption", "vwap_sd": None, "pnl": -0.56},
    {"time": "12:54", "symbol": "RPEL", "why": "Absorption", "vwap_sd": None, "pnl": 2.10},
    {"time": "12:57", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -1.30},
    {"time": "12:57", "symbol": "ASIANENE", "why": "Absorption", "vwap_sd": None, "pnl": -1.30},
    {"time": "13:00", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -3.76},
    {"time": "13:05", "symbol": "RPEL", "why": "Absorption", "vwap_sd": None, "pnl": 6.75},
    {"time": "13:33", "symbol": "HINDCOPPER", "why": "TAPESTALL+VWAP", "vwap_sd": 15.4, "pnl": -1.64},
    {"time": "13:36", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.47},
    {"time": "13:37", "symbol": "REDTAPE", "why": "Absorption", "vwap_sd": None, "pnl": 0.12},
    {"time": "13:37", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -1.06},
    {"time": "13:38", "symbol": "RTNINDIA", "why": "Absorption", "vwap_sd": None, "pnl": 0.61},
    {"time": "13:41", "symbol": "HINDCOPPER", "why": "TAPESTALL+VWAP", "vwap_sd": 24.7, "pnl": -0.99},
    {"time": "13:44", "symbol": "DATAPATTNS", "why": "TAPESTALL+VWAP", "vwap_sd": 12.2, "pnl": -3.95},
    {"time": "13:45", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -1.62},
    {"time": "13:47", "symbol": "DATAPATTNS", "why": "TAPESTALL+VWAP", "vwap_sd": 13.3, "pnl": -5.21},
    {"time": "13:51", "symbol": "HINDCOPPER", "why": "TAPESTALL+VWAP", "vwap_sd": 27.1, "pnl": -0.84},
    {"time": "13:56", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": 0.00},
    {"time": "14:00", "symbol": "PRECWIRE", "why": "Absorption", "vwap_sd": None, "pnl": 0.58},
    {"time": "14:06", "symbol": "PRECWIRE", "why": "Absorption", "vwap_sd": None, "pnl": -2.39},
    {"time": "14:07", "symbol": "MAHLOG", "why": "Absorption", "vwap_sd": None, "pnl": 4.45},
    {"time": "14:09", "symbol": "OIL", "why": "Absorption", "vwap_sd": None, "pnl": 0.00},
    {"time": "14:14", "symbol": "SHILCTECH", "why": "Absorption", "vwap_sd": None, "pnl": -13.49},
    {"time": "14:20", "symbol": "REDTAPE", "why": "EVENINGSTAR+VWAP", "vwap_sd": 15.2, "pnl": -1.30},
    {"time": "14:27", "symbol": "MAHLOG", "why": "Absorption", "vwap_sd": None, "pnl": 11.95},
    {"time": "14:31", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": 1.08},
    {"time": "14:31", "symbol": "TEJASNET", "why": "TAPESTALL+VWAP", "vwap_sd": 11.2, "pnl": -1.39},
    {"time": "14:34", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": 1.56},
    {"time": "14:34", "symbol": "PRECWIRE", "why": "TAPESTALL+VWAP", "vwap_sd": 12.3, "pnl": -0.48},
    {"time": "14:36", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": 1.65},
    {"time": "14:46", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.31},
    {"time": "14:47", "symbol": "WANBURY", "why": "Absorption", "vwap_sd": None, "pnl": 0.73},
    {"time": "14:49", "symbol": "HINDCOPPER", "why": "TAPESTALL+VWAP", "vwap_sd": 10.2, "pnl": -0.93},
    {"time": "14:51", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": -4.70},
    {"time": "14:57", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 1.40},
    {"time": "14:59", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": 2.25},
    {"time": "15:01", "symbol": "WANBURY", "why": "Absorption", "vwap_sd": None, "pnl": 3.10},
    {"time": "15:01", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": 2.00},
    {"time": "15:03", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 12.90},
    {"time": "15:03", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -1.35},
    {"time": "15:11", "symbol": "HINDCOPPER", "why": "Absorption", "vwap_sd": None, "pnl": -5.92},
    {"time": "15:13", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": 0.95},
    {"time": "15:13", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 3.20},
    {"time": "15:13", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": 0.30},
    {"time": "15:14", "symbol": "TEJASNET", "why": "Absorption", "vwap_sd": None, "pnl": -1.30},
    {"time": "15:14", "symbol": "OLECTRA", "why": "Absorption", "vwap_sd": None, "pnl": 1.80},
    {"time": "15:14", "symbol": "SCHNEIDER", "why": "Absorption", "vwap_sd": None, "pnl": -1.52},
    {"time": "15:15", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 4.10},
    {"time": "15:15", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.45},
    {"time": "15:16", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 0.50},
    {"time": "15:18", "symbol": "AVANTEL", "why": "Absorption", "vwap_sd": None, "pnl": -0.39},
    {"time": "15:19", "symbol": "PRECWIRE", "why": "EVENINGSTAR+VWAP", "vwap_sd": 7.9, "pnl": -0.72},
    {"time": "15:20", "symbol": "SPENCERS", "why": "Absorption", "vwap_sd": None, "pnl": -0.25},
    {"time": "15:20", "symbol": "BEML", "why": "Absorption", "vwap_sd": None, "pnl": 1.70},
    {"time": "15:23", "symbol": "HINDCOPPER", "why": "Absorption", "vwap_sd": None, "pnl": -1.25},
    {"time": "15:23", "symbol": "SPENCERS", "why": "Absorption", "vwap_sd": None, "pnl": -0.38},
    {"time": "15:25", "symbol": "SCHNEIDER", "why": "Absorption", "vwap_sd": None, "pnl": 1.70},
    {"time": "15:26", "symbol": "DATAPATTNS", "why": "Absorption", "vwap_sd": None, "pnl": 10.10},
    {"time": "15:27", "symbol": "HINDCOPPER", "why": "Absorption", "vwap_sd": None, "pnl": -0.85},
    {"time": "15:28", "symbol": "OLECTRA", "why": "Absorption", "vwap_sd": None, "pnl": 0.60},
]

def main():
    print("=" * 60)
    print("FILTER IMPACT ANALYSIS - Jan 28")
    print("=" * 60)
    
    all_pnl = sum(s['pnl'] for s in SIGNALS)
    all_wins = sum(1 for s in SIGNALS if s['pnl'] > 0)
    print(f"\n[CURRENT] All Signals: {len(SIGNALS)}")
    print(f"  Wins: {all_wins} | Losses: {len(SIGNALS) - all_wins}")
    print(f"  Win Rate: {all_wins/len(SIGNALS)*100:.1f}%")
    print(f"  Total P&L: {all_pnl:.2f} pts")
    
    # Filter: Only signals with VWAP SD (not generic Absorption)
    filter1 = [s for s in SIGNALS if s['vwap_sd'] is not None]
    f1_pnl = sum(s['pnl'] for s in filter1)
    f1_wins = sum(1 for s in filter1 if s['pnl'] > 0)
    
    print(f"\n[FILTER 1] Only Pattern+VWAP Signals: {len(filter1)}")
    if filter1:
        print(f"  Wins: {f1_wins} | Losses: {len(filter1) - f1_wins}")
        print(f"  Win Rate: {f1_wins/len(filter1)*100:.1f}%")
        print(f"  Total P&L: {f1_pnl:.2f} pts")
        print("  Details:")
        for s in filter1:
            sign = "WIN" if s['pnl'] > 0 else "LOSS"
            print(f"    [{sign}] {s['time']} {s['symbol']}: {s['pnl']:.2f} (VWAP +{s['vwap_sd']}SD)")
    
    # Filter: Block Absorption entirely
    filter2 = [s for s in SIGNALS if "Absorption" not in s['why']]
    f2_pnl = sum(s['pnl'] for s in filter2)
    f2_wins = sum(1 for s in filter2 if s['pnl'] > 0)
    
    print(f"\n[FILTER 2] Block ALL Absorption: {len(filter2)}")
    if filter2:
        print(f"  Wins: {f2_wins} | Losses: {len(filter2) - f2_wins}")
        print(f"  Win Rate: {f2_wins/len(filter2)*100:.1f}%")
        print(f"  Total P&L: {f2_pnl:.2f} pts")
    
    # First signal per symbol
    seen = set()
    filter3 = []
    for s in SIGNALS:
        if s['symbol'] not in seen:
            seen.add(s['symbol'])
            filter3.append(s)
    f3_pnl = sum(s['pnl'] for s in filter3)
    f3_wins = sum(1 for s in filter3 if s['pnl'] > 0)
    
    print(f"\n[FILTER 3] First Signal Per Symbol: {len(filter3)}")
    print(f"  Wins: {f3_wins} | Losses: {len(filter3) - f3_wins}")
    print(f"  Win Rate: {f3_wins/len(filter3)*100:.1f}%")
    print(f"  Total P&L: {f3_pnl:.2f} pts")
    
    print("\n" + "=" * 60)
    print("RECOMMENDATION:")
    print("  Use [FILTER 2] - Block Absorption patterns entirely")
    print("  This gives {} signals with {:.1f}% win rate".format(
        len(filter2), f2_wins/len(filter2)*100 if filter2 else 0))
    print("=" * 60)

if __name__ == "__main__":
    main()
