import pandas as pd
import datetime
import os
import logging
import uuid
import time
from database import DatabaseManager

logger = logging.getLogger(__name__)

JOURNAL_FILE = "data/trade_journal.csv"

class JournalManager:
    def __init__(self):
        self.file_path = JOURNAL_FILE
        self._ensure_file_exists()
        self.db = DatabaseManager() # Phase 41.3.2: SQLite Integration

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

    def log_entry(self, symbol, qty, price, reason, side="SHORT", hard_stop=0.0):
        """
        Logs a new trade entry to CSV and SQLite. Returns the TradeID (UUID).
        """
        try:
            trade_id = str(uuid.uuid4())[:8] # Short unique ID
            now = datetime.datetime.now()
            date_str = now.strftime("%Y-%m-%d")
            time_str = now.strftime("%H:%M:%S")

            # 1. CSV Logging (Legacy/Backup)
            df = pd.read_csv(self.file_path)
            new_row = {
                "TradeID": trade_id,
                "Date": date_str,
                "Symbol": symbol,
                "Direction": side,
                "EntryTime": time_str,
                "EntryPrice": price,
                "Qty": qty,
                "ExitTime": "",
                "ExitPrice": 0.0,
                "PnL": 0.0,
                "PnL_Pct": 0.0,
                "Status": "OPEN",
                "Reason": reason
            }
            df = pd.concat([df, pd.DataFrame([new_row])], ignore_index=True)
            df.to_csv(self.file_path, index=False)
            
            # 2. SQLite Logging (Phase 41.3.2)
            db_record = {
                'trade_id_str': trade_id,
                'date': date_str,
                'symbol': symbol,
                'direction': side,
                'entry_time': now, # Pass datetime object
                'entry_price': price,
                'qty': qty,
                'status': 'OPEN',
                'hard_stop_price': hard_stop,
                'phase': '41.3'
            }
            self.db.log_trade_entry(db_record)
            
            logger.info(f"📓 Journal Entry Logged: {symbol} @ {price} ({trade_id})")
            return trade_id
            
        except Exception as e:
            logger.error(f"Failed to log entry: {e}")
            return None

    def log_exit(self, trade_id, exit_price, exit_reason="MANUAL"):
        """
        Updates an existing trade with exit details in CSV and SQLite.
        """
        try:
            # 1. CSV Update
            df = pd.read_csv(self.file_path)
            mask = df['TradeID'] == trade_id
            if not mask.any():
                logger.error(f"TradeID {trade_id} not found in journal.")
                return None
                
            idx = df.index[mask][0]
            entry_price = float(df.at[idx, 'EntryPrice'])
            qty = int(df.at[idx, 'Qty'])
            direction = df.at[idx, 'Direction']
            
            if direction == "SHORT":
                pnl = (entry_price - exit_price) * qty
                pnl_pct = ((entry_price - exit_price) / entry_price) * 100
            else:
                pnl = (exit_price - entry_price) * qty
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                
            now = datetime.datetime.now()
            df.at[idx, 'ExitTime'] = now.strftime("%H:%M:%S")
            df.at[idx, 'ExitPrice'] = round(exit_price, 2)
            df.at[idx, 'PnL'] = round(pnl, 2)
            df.at[idx, 'PnL_Pct'] = round(pnl_pct, 2)
            df.at[idx, 'Status'] = "CLOSED"
            df.at[idx, 'Reason'] = exit_reason # Update exit reason if needed, or keep entry reason? 
            # CSV structure has 'Reason' usually for entry, but let's assume valid usage.
            # Actually, standard is Entry Reason. exit_reason is usually separate.
            # I'll append exit reason to Reason column or ignore for CSV to avoid schema change.
            
            df.to_csv(self.file_path, index=False)
            
            # 2. SQLite Update
            exit_data = {
                'exit_time': now,
                'exit_price': exit_price,
                'pnl': pnl,
                'pnl_pct': pnl_pct,
                'exit_reason': exit_reason
            }
            self.db.log_trade_exit(trade_id, exit_data)
            
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
        """
        try:
            # Retrieve from CSV for now, simpler than querying DB for ID
            # Could switch to DB query, but CSV is memory loaded here.
            df = pd.read_csv(self.file_path)
            open_trades = df[(df['Symbol'] == symbol) & (df['Status'] == 'OPEN')]
            
            if not open_trades.empty:
                return open_trades.iloc[-1]['TradeID']
            return None
        except:
            return None
