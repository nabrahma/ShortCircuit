"""
EOD Outcome Labeler
Run this script at end of day (after 15:30) to label ML training data.

Usage:
    python scripts/label_outcomes.py

What it does:
1. Reads today's observations from data/ml/
2. For each signal, fetches the closing price
3. Labels WIN/LOSS/BREAKEVEN based on P&L
4. Updates the parquet file with outcomes
"""

import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from fyers_apiv3 import fyersModel

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Config
DATA_DIR = Path("data/ml")
WIN_THRESHOLD_PCT = 0.5  # 0.5% move in our favor = WIN
LOSS_THRESHOLD_PCT = -0.5  # 0.5% move against = LOSS


def load_fyers_client():
    """Load Fyers client from saved token."""
    from dotenv import load_dotenv
    load_dotenv()
    
    client_id = os.getenv("FYERS_CLIENT_ID")
    access_token_path = Path("access_token.txt")
    
    if not access_token_path.exists():
        logger.error("No access_token.txt found. Run main.py first to authenticate.")
        return None
    
    access_token = access_token_path.read_text().strip()
    
    fyers = fyersModel.FyersModel(
        client_id=client_id,
        token=access_token,
        is_async=False,
        log_path=""
    )
    
    return fyers


def get_closing_price(fyers, symbol: str, signal_time: str) -> tuple:
    """
    Get the closing price and MFE/MAE for a symbol.
    Returns: (close_price, max_favorable, max_adverse)
    """
    try:
        # Parse signal time
        signal_dt = datetime.strptime(signal_time, "%H:%M:%S")
        today = datetime.now().date()
        signal_datetime = datetime.combine(today, signal_dt.time())
        
        # Fetch 1-min data from signal time to close
        data = {
            "symbol": symbol,
            "resolution": "1",
            "date_format": "1",
            "range_from": signal_datetime.strftime("%Y-%m-%d"),
            "range_to": signal_datetime.strftime("%Y-%m-%d"),
            "cont_flag": "1"
        }
        
        response = fyers.history(data=data)
        
        if 'candles' not in response or not response['candles']:
            logger.warning(f"No candle data for {symbol}")
            return None, None, None
        
        candles = response['candles']
        df = pd.DataFrame(candles, columns=['epoch', 'open', 'high', 'low', 'close', 'volume'])
        df['datetime'] = pd.to_datetime(df['epoch'], unit='s')
        
        # Filter to after signal time
        # (candles are in IST, need to handle timezone)
        df['datetime'] = df['datetime'].dt.tz_localize('UTC').dt.tz_convert('Asia/Kolkata')
        
        # Get close (15:30 or last candle)
        close_price = df.iloc[-1]['close']
        
        # Calculate MFE (max favorable - for shorts, this is lowest low)
        # and MAE (max adverse - for shorts, this is highest high)
        min_low = df['low'].min()
        max_high = df['high'].max()
        
        return close_price, min_low, max_high
        
    except Exception as e:
        logger.error(f"Error fetching close for {symbol}: {e}")
        return None, None, None


def label_observation(entry_price: float, close_price: float, max_low: float, max_high: float) -> dict:
    """
    Calculate outcome labels for a SHORT trade.
    """
    # P&L (for shorts: entry - close)
    pnl_pct = ((entry_price - close_price) / entry_price) * 100
    
    # MFE/MAE (for shorts)
    mfe = ((entry_price - max_low) / entry_price) * 100  # Best case (price went down)
    mae = ((max_high - entry_price) / entry_price) * 100  # Worst case (price went up)
    
    # Outcome
    if pnl_pct >= WIN_THRESHOLD_PCT:
        outcome = "WIN"
    elif pnl_pct <= LOSS_THRESHOLD_PCT:
        outcome = "LOSS"
    else:
        outcome = "BREAKEVEN"
    
    return {
        "outcome": outcome,
        "exit_price": close_price,
        "max_favorable": mfe,
        "max_adverse": mae,
        "pnl_pct": pnl_pct
    }


def main():
    """Main labeling routine."""
    today = datetime.now().strftime("%Y-%m-%d")
    today_file = DATA_DIR / f"observations_{today}.parquet"
    
    if not today_file.exists():
        logger.warning(f"No observations file for today: {today_file}")
        logger.info("Nothing to label. Run the bot during market hours first.")
        return
    
    # Load observations
    df = pd.read_parquet(today_file)
    unlabeled = df[df['outcome'].isna()]
    
    if len(unlabeled) == 0:
        logger.info("All observations already labeled!")
        return
    
    logger.info(f"Found {len(unlabeled)} unlabeled observations")
    
    # Load Fyers client
    fyers = load_fyers_client()
    if not fyers:
        return
    
    # Label each observation
    updated = 0
    for idx, row in unlabeled.iterrows():
        symbol = row['symbol']
        entry_price = row['ltp']
        signal_time = row['time']
        
        logger.info(f"Labeling {symbol} (Entry: {entry_price})...")
        
        close_price, max_low, max_high = get_closing_price(fyers, symbol, signal_time)
        
        if close_price is None:
            logger.warning(f"  Skipping {symbol} - no close data")
            continue
        
        labels = label_observation(entry_price, close_price, max_low, max_high)
        
        # Update DataFrame
        for key, value in labels.items():
            df.loc[idx, key] = value
        
        logger.info(f"  {symbol}: {labels['outcome']} ({labels['pnl_pct']:.2f}%)")
        updated += 1
    
    # Save updated file
    df.to_parquet(today_file, index=False)
    df.to_csv(today_file.with_suffix('.csv'), index=False)
    
    logger.info(f"\nLabeled {updated} observations. File saved: {today_file}")
    
    # Summary stats
    labeled = df[df['outcome'].notna()]
    if len(labeled) > 0:
        wins = len(labeled[labeled['outcome'] == 'WIN'])
        losses = len(labeled[labeled['outcome'] == 'LOSS'])
        be = len(labeled[labeled['outcome'] == 'BREAKEVEN'])
        win_rate = wins / len(labeled) * 100
        
        print(f"\n{'='*40}")
        print(f"TODAY'S STATS ({today})")
        print(f"{'='*40}")
        print(f"Total Signals: {len(labeled)}")
        print(f"Wins: {wins} | Losses: {losses} | BE: {be}")
        print(f"Win Rate: {win_rate:.1f}%")
        print(f"Avg P&L: {labeled['pnl_pct'].mean():.2f}%")
        print(f"Best Trade: {labeled['pnl_pct'].max():.2f}%")
        print(f"Worst Trade: {labeled['pnl_pct'].min():.2f}%")
        print(f"{'='*40}")


if __name__ == "__main__":
    main()
