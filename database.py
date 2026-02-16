import sqlite3
import logging
import datetime
import os
import json

logger = logging.getLogger(__name__)

DB_FILE = "data/short_circuit.db"

class DatabaseManager:
    """
    Phase 41.3.2: Central Database Manager using SQLite.
    Handles all data persistence for Trades, Events, and EOD Analysis.
    """
    
    def __init__(self):
        self.db_path = DB_FILE
        self._ensure_db_dir()
        self._init_db()

    def _ensure_db_dir(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)

    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)

    def _init_db(self):
        """Initialize Database Schema"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # 1. Trades Table (Core)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id_str TEXT UNIQUE,
                date DATE,
                symbol TEXT,
                direction TEXT,
                entry_time DATETIME,
                entry_price REAL,
                qty INTEGER,
                exit_time DATETIME,
                exit_price REAL,
                pnl REAL,
                pnl_pct REAL,
                status TEXT,
                exit_reason TEXT,
                hard_stop_price REAL,
                phase TEXT DEFAULT '41.3'
            )
        ''')
        
        # 2. Trade Events (Replay Log)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trade_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                timestamp DATETIME,
                event_type TEXT,
                price REAL,
                orderflow INTEGER,
                volume INTEGER,
                price_tests INTEGER,
                liquidity INTEGER,
                mtf INTEGER,
                velocity INTEGER,
                bearish_count INTEGER,
                bullish_count INTEGER,
                decision TEXT,
                reason TEXT,
                momentum_score INTEGER,
                FOREIGN KEY (trade_id) REFERENCES trades(trade_id_str)
            )
        ''')
        
        # 3. Soft Stop Events (Analysis)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS soft_stop_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trade_id TEXT,
                date DATE,
                symbol TEXT,
                entry_price REAL,
                soft_stop_trigger_price REAL,
                soft_stop_decision TEXT,
                exit_price REAL,
                exit_reason TEXT,
                orderflow_signal INTEGER,
                volume_signal INTEGER,
                price_tests_signal INTEGER,
                liquidity_signal INTEGER,
                mtf_signal INTEGER,
                velocity_signal INTEGER,
                signal_score INTEGER,
                time_in_trade_minutes INTEGER,
                outcome TEXT,
                FOREIGN KEY (trade_id) REFERENCES trades(trade_id_str)
            )
        ''')
        
        # 4. Daily Summaries (EOD Report)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS daily_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date DATE UNIQUE,
                phase TEXT,
                total_trades INTEGER,
                winners INTEGER,
                losers INTEGER,
                win_rate REAL,
                total_pnl REAL,
                avg_pnl_pct REAL,
                avg_win_pct REAL,
                avg_loss_pct REAL,
                profit_factor REAL,
                max_loss_pct REAL,
                phantom_fills INTEGER,
                orphaned_orders INTEGER,
                safety_status TEXT,
                best_signal_1 TEXT,
                best_signal_2 TEXT,
                signal_accuracy_1 REAL,
                signal_accuracy_2 REAL,
                profit_capture_pct REAL
            )
        ''')

        conn.commit()
        conn.close()
        logger.info("✅ Database Schema Initialized.")

    # ── TRADE OPERATIONS ────────────────────────────────────────

    def log_trade_entry(self, trade_data):
        """
        Log new trade entry.
        trade_data dict must match schema columns.
        Returns: internal DB ID
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Map dict keys to schema
            cols = ['trade_id_str', 'date', 'symbol', 'direction', 'entry_time', 'entry_price', 'qty', 'status', 'hard_stop_price', 'phase']
            vals = [trade_data.get(c) for c in cols]
            
            cursor.execute(f'''
                INSERT INTO trades ({','.join(cols)})
                VALUES ({','.join(['?']*len(cols))})
            ''', vals)
            
            conn.commit()
            return cursor.lastrowid
        except Exception as e:
            logger.error(f"DB Entry Error: {e}")
            return None
        finally:
            conn.close()

    def log_trade_exit(self, trade_id_str, exit_data):
        """
        Update trade with exit details.
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # Calculate PnL if not provided? Assuming caller provides final values.
            # We update by trade_id_str (UUID)
            cursor.execute('''
                UPDATE trades
                SET exit_time = ?, exit_price = ?, pnl = ?, pnl_pct = ?, status = 'CLOSED', exit_reason = ?
                WHERE trade_id_str = ?
            ''', (exit_data['exit_time'], exit_data['exit_price'], exit_data['pnl'], exit_data['pnl_pct'], exit_data['exit_reason'], trade_id_str))
            
            conn.commit()
        except Exception as e:
            logger.error(f"DB Exit Error: {e}")
        finally:
            conn.close()

    # ── ANALYTICS OPERATIONS ────────────────────────────────────
    
    def log_event(self, table, data):
        """Generic event logger"""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            keys = ", ".join(data.keys())
            placeholders = ", ".join(["?"] * len(data))
            values = list(data.values())
            
            cursor.execute(f"INSERT INTO {table} ({keys}) VALUES ({placeholders})", values)
            conn.commit()
        except Exception as e:
            logger.error(f"DB Event Error ({table}): {e}")
        finally:
            conn.close()
            
    def query(self, query, args=()):
        """Run updated query and return dicts"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute(query, args)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"DB Query Error: {e}")
            return []
        finally:
            conn.close()
