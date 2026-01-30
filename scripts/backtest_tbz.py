import yfinance as yf
import pandas_ta as ta
import pandas as pd
import logging
from telegram_bot import TelegramBot

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def backtest_tbz():
    symbol = "TBZ.NS"
    logger.info(f"Backtesting {symbol} for today...")
    
    # 1. Fetch Data
    df = yf.download(symbol, period="1d", interval="1m", progress=False)
    
    if df.empty:
        logger.error("No data found for TBZ.NS")
        return

    # Clean Columns
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df.rename(columns={"Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"}, inplace=True)

    # 2. Indicators
    df.ta.vwap(append=True)
    vwap_col = 'VWAP_D'
    if vwap_col not in df.columns:
         cols = [c for c in df.columns if 'VWAP' in c]
         if cols: vwap_col = cols[0]
         
    df['VOL_SMA'] = ta.sma(df['volume'], length=10)

    # 3. Iterate looking for signals
    signals = []
    
    # Start from index 20 to have enough history for SMA/Swing
    for i in range(20, len(df)):
        current = df.iloc[i]
        prev = df.iloc[i-1]
        
        # Convert timestamp to IST
        # yfinance usually returns UTC-aware (if interval < 1d) or naive.
        # Check if tz-aware.
        timestamp = current.name
        if timestamp.tzinfo is None:
             # Assume UTC if naive, or localize
             timestamp = timestamp.tz_localize('UTC')
        
        timestamp = timestamp.tz_convert('Asia/Kolkata')

        price = current['close']
        
        # A. Price > VWAP check (Strategy Requirement)
        if price <= current[vwap_col]:
            continue

        # B. Pattern Logic
        pattern = None
        
        body = abs(current['close'] - current['open'])
        upper_wick = current['high'] - max(current['open'], current['close'])
        lower_wick = min(current['open'], current['close']) - current['low']
        
        # 1. Shooting Star
        if upper_wick > (2 * body) and lower_wick < body:
            pattern = "Shooting Star"

        # 2. Bearish Engulfing
        if not pattern:
            prev_body = abs(prev['close'] - prev['open'])
            is_prev_green = prev['close'] > prev['open']
            is_curr_red = current['close'] < current['open']
            if is_prev_green and is_curr_red:
                if current['open'] >= prev['close'] and current['close'] <= prev['open']:
                    pattern = "Bearish Engulfing"
        
        # 3. Hanging Man
        if not pattern:
            if lower_wick > (2 * body) and upper_wick < body:
                pattern = "Hanging Man"
        
        # 4. ICT Sweep (Simplified window lookup for backtest efficiency)
        if not pattern:
            window = df.iloc[i-16:i-1] # 15 candles before current
            swing_high = window['high'].max()
            if current['high'] > swing_high and current['close'] < swing_high:
                pattern = "ICT Liquidity Sweep"

        if pattern:
            signals.append(f"🕒 {timestamp.strftime('%H:%M')} | {pattern} @ {price:.2f}")

    # 4. Report
    bot = TelegramBot()
    
    if signals:
        summary = "\n".join(signals[:10]) # Limit to first 10 to avoid spam
        if len(signals) > 10: summary += f"\n... and {len(signals)-10} more."
        
        msg = (
            f"🔍 **Backtest Result: {symbol} (Today)**\n"
            f"-----------------------------\n"
            f"{summary}\n"
            f"-----------------------------\n"
            f"Total Signals: {len(signals)}"
        )
        print(msg)
        bot.bot.send_message(bot.chat_id, msg, parse_mode="Markdown")
        print("Sent to Telegram.")
    else:
        msg = f"🔍 **Backtest Result: {symbol}**\nNo signals found properly satisfying conditions (Price > VWAP + Pattern)."
        print(msg)
        bot.bot.send_message(bot.chat_id, msg, parse_mode="Markdown")

if __name__ == "__main__":
    backtest_tbz()
