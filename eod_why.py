"""
Phase 42.2: Missed Opportunity CLI Tool

Usage:
    python eod_why.py RELIANCE 14:25
    python eod_why.py NSE:TATASTEEL-EQ 11:30
    python eod_why.py INFY 2026-02-15 10:45:00

Analyzes WHY the bot did not signal a stock at the given time.
"""

import sys
import logging
from datetime import datetime

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


def format_gate_result(gate: dict) -> str:
    """Pretty-print a gate result."""
    status = gate['status']
    if status == 'PASSED':
        icon = '✅'
    elif status == 'FAILED':
        icon = '❌'
    elif status == 'ERROR':
        icon = '⚠️'
    else:
        icon = '⏭️'

    output = f"\n{icon} Gate {gate['gate_num']}: {gate['name']}"
    output += f"\n   Status: {status}"

    if gate.get('reason') and gate['reason'] != 'OK':
        output += f"\n   Reason: {gate['reason']}"

    if gate.get('details'):
        for key, val in gate['details'].items():
            if isinstance(val, list):
                val = ', '.join(str(v) for v in val) if val else '(none)'
            output += f"\n   {key}: {val}"

    if gate.get('suggestion'):
        output += f"\n   💡 {gate['suggestion']}"

    return output


def main():
    if len(sys.argv) < 3:
        print("\nUsage: python eod_why.py SYMBOL TIME [DATE]")
        print("\nExamples:")
        print("  python eod_why.py RELIANCE 14:25")
        print("  python eod_why.py TATASTEEL 11:30")
        print("  python eod_why.py NSE:INFY-EQ 10:45")
        print("  python eod_why.py RELIANCE 14:25 2026-02-14")
        sys.exit(1)

    symbol = sys.argv[1]
    time_str = sys.argv[2]

    # Optional date argument (for past dates)
    if len(sys.argv) >= 4:
        date_str = sys.argv[3]
        time_str = f"{date_str} {time_str}:00"

    print(f"\n{'='*70}")
    print(f"🔍 MISSED OPPORTUNITY ANALYSIS")
    print(f"{'='*70}\n")
    print(f"Stock: {symbol}")
    print(f"Time:  {time_str}")
    print(f"\nConnecting to Fyers & fetching data...")

    # Authenticate
    try:
        from fyers_connect import FyersConnect
        conn = FyersConnect()
        fyers = conn.authenticate()
    except Exception as e:
        print(f"\n❌ Fyers authentication failed: {e}")
        print("Make sure you have a valid auth token.")
        sys.exit(1)

    # Run diagnostic
    from diagnostic_analyzer import DiagnosticAnalyzer
    analyzer = DiagnosticAnalyzer(fyers)
    result = analyzer.analyze_missed_opportunity(symbol, time_str)

    if 'error' in result:
        print(f"\n❌ Error: {result['error']}")
        sys.exit(1)

    # Display results
    print(f"\nPrice at Analysis: ₹{result['ltp_at_analysis']:.2f}")
    print(f"Day High: ₹{result['day_high']:.2f}")
    print(f"Day Gain: +{result['day_gain']:.2f}%")

    print(f"\n{'='*70}")
    print(f"SIGNAL PIPELINE DIAGNOSTIC (12 Gates)")
    print(f"{'='*70}")

    for gate in result['gates']:
        print(format_gate_result(gate))

    # Summary
    passed_count = sum(1 for g in result['gates'] if g['status'] == 'PASSED')
    failed_count = sum(1 for g in result['gates'] if g['status'] == 'FAILED')

    print(f"\n{'='*70}")
    print(f"VERDICT: {passed_count} passed, {failed_count} failed")
    print(f"{'='*70}")

    if result['passed_all_gates']:
        print(f"\n✅ PASSED ALL GATES — Signal SHOULD have been generated!")
        print(f"   Possible reasons it wasn't:")
        print(f"   → Bot wasn't running at this time")
        print(f"   → Stock wasn't in scanner's top gainers list")
        print(f"   → Validation gate timed out")
        print(f"   → Race condition / timing mismatch")
    else:
        failed_gate = result['gates'][result['first_failure_gate'] - 1]
        print(f"\n❌ STOPPED AT GATE {result['first_failure_gate']}: {failed_gate['name']}")
        print(f"   Reason: {failed_gate.get('reason', 'Unknown')}")
        if failed_gate.get('suggestion'):
            print(f"   💡 {failed_gate['suggestion']}")

    # Profitability check
    prof = result.get('profitability', {})
    if prof.get('available'):
        print(f"\n{'='*70}")
        print(f"PROFITABILITY CHECK (30 min later)")
        print(f"{'='*70}")
        print(f"Entry (Short): ₹{prof['entry_price']:.2f}")
        print(f"Lowest Price:  ₹{prof['low_30min']:.2f}  (Max profit: {prof['max_profit_pct']:+.2f}%)")
        print(f"Price at +30m: ₹{prof['close_30min']:.2f}  (P&L: {prof['exit_profit_pct']:+.2f}%)")

        if prof['would_be_profitable']:
            print(f"\n🎯 This WOULD have been profitable (+{prof['exit_profit_pct']:.2f}%)")
        else:
            print(f"\n❌ This would have been a LOSS ({prof['exit_profit_pct']:+.2f}%)")
    else:
        print(f"\n⚠️  Could not fetch post-signal price data for profitability check.")

    print(f"\n{'='*70}\n")


if __name__ == '__main__':
    main()
