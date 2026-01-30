import pandas as pd
import datetime
import os
import logging
import uuid
import time

logger = logging.getLogger(__name__)

JOURNAL_FILE = "data/trade_journal.csv"

class JournalManager:
    def __init__(self):
        self.file_path = JOURNAL_FILE
        self._ensure_file_exists()

    def _ensure_file_exists(self):
        # Ensure data directory exists
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        
        if not os.path.exists(self.file_path):
            df = pd.DataFrame(columns=[
                "TradeID", "Date", "Symbol", "Direction", 
                "EntryTime", "EntryPrice", "Qty", 
                "ExitTime", "ExitPrice", "PnL", "PnL_Pct", "Status", "Reason"
            ])
            df.to_csv(self.file_path, index=False)

    def log_entry(self, symbol, qty, price, reason, side="SHORT"):
        """
        Logs a new trade entry. Returns the TradeID.
        """
        try:
            df = pd.read_csv(self.file_path)
            
            trade_id = str(uuid.uuid4())[:8] # Short unique ID
            now = datetime.datetime.now()
            
            new_row = {
                "TradeID": trade_id,
                "Date": now.strftime("%Y-%m-%d"),
                "Symbol": symbol,
                "Direction": side,
                "EntryTime": now.strftime("%H:%M:%S"),
                "EntryPrice": price,
                "Qty": qty,
                "ExitTime": "",
                "ExitPrice": 0.0,
                "PnL": 0.0,
                "PnL_Pct": 0.0,
                "Status": "OPEN",
                "Reason": reason
            }
            
            # Append using loc for pandas > 2.0 or concat
            # To avoid FutureWarning with frame.append
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.file_path, index=False)
            
            logger.info(f"📓 Journal Entry Logged: {symbol} @ {price} ({trade_id})")
            return trade_id
            
        except Exception as e:
            logger.error(f"Failed to log entry: {e}")
            return None

    def log_exit(self, trade_id, exit_price):
        """
        Updates an existing trade with exit details.
        """
        try:
            df = pd.read_csv(self.file_path)
            
            # Find the row with TradeID
            mask = df['TradeID'] == trade_id
            if not mask.any():
                logger.error(f"TradeID {trade_id} not found in journal.")
                return None
                
            # Get Entry Details
            idx = df.index[mask][0]
            entry_price = float(df.at[idx, 'EntryPrice'])
            qty = int(df.at[idx, 'Qty'])
            direction = df.at[idx, 'Direction']
            
            # Calc P&L
            # Short: Entry - Exit
            if direction == "SHORT":
                pnl = (entry_price - exit_price) * qty
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100
            else: # LONG (Future proof)
                pnl = (exit_price - entry_price) * qty
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                
            # Update Row
            now = datetime.datetime.now()
            df.at[idx, 'ExitTime'] = now.strftime("%H:%M:%S")
            df.at[idx, 'ExitPrice'] = round(exit_price, 2)
            df.at[idx, 'PnL'] = round(pnl, 2)
            df.at[idx, 'PnL_Pct'] = round(pnl_pct, 2)
            df.at[idx, 'Status'] = "CLOSED"
            
            df.to_csv(self.file_path, index=False)
            logger.info(f"📓 Journal Exit Logged: {trade_id} P&L: {pnl}")
            
            return {
                "pnl": pnl,
                "pnl_pct": pnl_pct,
                "entry": entry_price,
                "exit": exit_price
            }
            
        except Exception as e:
            logger.error(f"Failed to log exit: {e}")
            return None

    def get_open_trade(self, symbol):
        """
        Helper to find the most recent OPEN trade for a symbol.
        Used if we want to close by Symbol instead of ID (optional safety).
        """
        try:
            df = pd.read_csv(self.file_path)
            # Filter by Symbol and Status=OPEN
            open_trades = df[(df['Symbol'] == symbol) & (df['Status'] == 'OPEN')]
            
            if not open_trades.empty:
                # Return the last one
                return open_trades.iloc[-1]['TradeID']
            return None
        except:
            return None
