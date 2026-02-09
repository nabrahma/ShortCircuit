"""
ML Data Logger Module
Collects training data for reversal pattern prediction.

Best Practices Implemented:
1. Parquet format (efficient, typed, ML-ready)
2. Atomic writes (no corruption on crash)
3. Schema versioning
4. Unique observation IDs
5. Separate feature logging from outcome logging
6. Daily file rotation
7. Automatic backup

Usage:
    logger = MLDataLogger()
    obs_id = logger.log_observation(symbol, features)  # At signal time
    logger.update_outcome(obs_id, outcome)  # EOD
"""

import os
import json
import uuid
import logging
import datetime
import threading
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

logger = logging.getLogger("MLDataLogger")

# Schema version - increment when changing feature set
SCHEMA_VERSION = "1.0.0"

# Feature columns for ML training
FEATURE_COLUMNS = [
    # Identifiers
    "obs_id",           # Unique observation ID
    "schema_version",   # For backward compatibility
    "date",             # YYYY-MM-DD
    "time",             # HH:MM:SS
    "symbol",           # NSE:SYMBOL-EQ
    
    # Price Context
    "ltp",              # Last traded price at signal
    "prev_close",       # Previous day close
    "day_high",         # Day high at signal time
    "day_low",          # Day low at signal time
    "gain_pct",         # % gain from prev close
    
    # VWAP Features
    "vwap",             # Current VWAP
    "vwap_distance_pct", # Distance from VWAP as %
    "vwap_sd",          # Standard deviations from VWAP
    "vwap_slope",       # VWAP slope (trend)
    
    # Volume Features
    "volume_current",   # Volume of setup candle
    "volume_avg_20",    # 20-candle average volume
    "rvol",             # Relative volume (current/avg)
    
    # Structure Features
    "pattern",          # Pattern type (SHOOTING_STAR, etc.)
    "candle_body_pct",  # Body as % of range
    "upper_wick_pct",   # Upper wick as % of range
    "lower_wick_pct",   # Lower wick as % of range
    
    # DOM/Orderflow
    "dom_ratio",        # Sell qty / Buy qty
    "bid_ask_spread",   # Spread in %
    
    # Confluence Count
    "num_confirmations", # Number of pro_conf items
    "confirmations",    # JSON list of confirmations
    
    # Orderflow Flags
    "is_round_number",  # Near psychological level
    "is_bad_high",      # Heavy sellers at high
    "is_trapped",       # Trapped positions detected
    "is_absorption",    # Aggression no progress
    
    # Context
    "nifty_trend",      # UP/DOWN/RANGE
    "sector",           # Extracted from symbol if possible
    "time_bucket",      # OPEN/MID/CLOSE (market phase)
    
    # OI (if available)
    "oi",               # Open interest
    "oi_change_pct",    # OI change from previous
    
    # Labels (filled at EOD)
    "outcome",          # WIN/LOSS/BREAKEVEN
    "exit_price",       # Actual or simulated exit
    "max_favorable",    # Max favorable excursion (MFE)
    "max_adverse",      # Max adverse excursion (MAE)
    "pnl_pct",          # P&L as % of entry
    "hold_time_mins",   # How long position held
]

class MLDataLogger:
    """
    Production-grade ML data logger.
    Logs observations at signal time, updates outcomes at EOD.
    """
    
    def __init__(self, data_dir: str = "data/ml"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        # Daily file path
        self.today = datetime.date.today().isoformat()
        self.daily_file = self.data_dir / f"observations_{self.today}.parquet"
        self.backup_file = self.data_dir / f"observations_{self.today}.csv"
        
        # In-memory buffer for atomic writes
        self._buffer: list = []
        self._lock = threading.Lock()
        
        # Load existing data if any
        self._load_existing()
        
        logger.info(f"[ML] Data logger initialized. File: {self.daily_file}")
    
    def _load_existing(self):
        """Load existing observations for today if file exists."""
        if self.daily_file.exists():
            try:
                df = pd.read_parquet(self.daily_file)
                self._buffer = df.to_dict('records')
                logger.info(f"[ML] Loaded {len(self._buffer)} existing observations")
            except Exception as e:
                logger.error(f"[ML] Error loading existing data: {e}")
                # Try backup
                if self.backup_file.exists():
                    df = pd.read_csv(self.backup_file)
                    self._buffer = df.to_dict('records')
    
    def _save(self):
        """Atomic save to parquet + CSV backup."""
        with self._lock:
            if not self._buffer:
                return
            
            try:
                df = pd.DataFrame(self._buffer)
                
                # Parquet (primary)
                temp_file = self.daily_file.with_suffix('.tmp')
                df.to_parquet(temp_file, index=False)
                temp_file.replace(self.daily_file)
                
                # CSV backup
                df.to_csv(self.backup_file, index=False)
                
            except Exception as e:
                logger.error(f"[ML] Save error: {e}")
    
    def log_observation(
        self,
        symbol: str,
        ltp: float,
        features: Dict[str, Any]
    ) -> str:
        """
        Log a new observation when a signal is detected.
        Returns observation ID for later outcome update.
        """
        obs_id = str(uuid.uuid4())[:8]  # Short unique ID
        now = datetime.datetime.now()
        
        # Determine time bucket
        hour = now.hour
        if hour < 10:
            time_bucket = "OPEN"
        elif hour < 14:
            time_bucket = "MID"
        else:
            time_bucket = "CLOSE"
        
        observation = {
            # Identifiers
            "obs_id": obs_id,
            "schema_version": SCHEMA_VERSION,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "symbol": symbol,
            
            # From features dict
            "ltp": ltp,
            "prev_close": features.get("prev_close", 0),
            "day_high": features.get("day_high", ltp),
            "day_low": features.get("day_low", ltp),
            "gain_pct": features.get("gain_pct", 0),
            
            "vwap": features.get("vwap", 0),
            "vwap_distance_pct": features.get("vwap_distance_pct", 0),
            "vwap_sd": features.get("vwap_sd", 0),
            "vwap_slope": features.get("vwap_slope", 0),
            
            "volume_current": features.get("volume_current", 0),
            "volume_avg_20": features.get("volume_avg_20", 0),
            "rvol": features.get("rvol", 0),
            
            "pattern": features.get("pattern", "UNKNOWN"),
            "candle_body_pct": features.get("candle_body_pct", 0),
            "upper_wick_pct": features.get("upper_wick_pct", 0),
            "lower_wick_pct": features.get("lower_wick_pct", 0),
            
            "dom_ratio": features.get("dom_ratio", 1.0),
            "bid_ask_spread": features.get("bid_ask_spread", 0),
            
            "num_confirmations": features.get("num_confirmations", 0),
            "confirmations": json.dumps(features.get("confirmations", [])),
            
            "is_round_number": features.get("is_round_number", False),
            "is_bad_high": features.get("is_bad_high", False),
            "is_trapped": features.get("is_trapped", False),
            "is_absorption": features.get("is_absorption", False),
            
            "nifty_trend": features.get("nifty_trend", "UNKNOWN"),
            "sector": self._extract_sector(symbol),
            "time_bucket": time_bucket,
            
            "oi": features.get("oi", 0),
            "oi_change_pct": features.get("oi_change_pct", 0),
            
            # Labels (empty, filled at EOD)
            "outcome": None,
            "exit_price": None,
            "max_favorable": None,
            "max_adverse": None,
            "pnl_pct": None,
            "hold_time_mins": None,
        }
        
        with self._lock:
            self._buffer.append(observation)
        
        self._save()
        logger.info(f"[ML] Logged observation {obs_id} for {symbol}")
        
        return obs_id
    
    def update_outcome(
        self,
        obs_id: str,
        outcome: str,
        exit_price: float,
        max_favorable: float = 0,
        max_adverse: float = 0,
        hold_time_mins: int = 0
    ):
        """
        Update the outcome for an observation (called at EOD or trade close).
        """
        with self._lock:
            for obs in self._buffer:
                if obs["obs_id"] == obs_id:
                    entry = obs["ltp"]
                    pnl_pct = ((entry - exit_price) / entry) * 100 if entry > 0 else 0
                    
                    obs["outcome"] = outcome
                    obs["exit_price"] = exit_price
                    obs["max_favorable"] = max_favorable
                    obs["max_adverse"] = max_adverse
                    obs["pnl_pct"] = pnl_pct
                    obs["hold_time_mins"] = hold_time_mins
                    
                    logger.info(f"[ML] Updated outcome for {obs_id}: {outcome} ({pnl_pct:.2f}%)")
                    break
        
        self._save()
    
    def _extract_sector(self, symbol: str) -> str:
        """Extract sector from symbol (simplified)."""
        # Could be enhanced with a sector mapping file
        symbol_clean = symbol.replace("NSE:", "").replace("-EQ", "")
        
        # Basic sector keywords
        if any(x in symbol_clean for x in ["BANK", "FIN", "HDFC", "ICICI", "KOTAK"]):
            return "BANKING"
        elif any(x in symbol_clean for x in ["STEEL", "TATA", "JSW", "JINDAL"]):
            return "METAL"
        elif any(x in symbol_clean for x in ["PHARMA", "SUN", "CIPLA", "DR"]):
            return "PHARMA"
        elif any(x in symbol_clean for x in ["TECH", "INFY", "TCS", "WIPRO"]):
            return "IT"
        elif any(x in symbol_clean for x in ["OIL", "RELIANCE", "ONGC", "BPCL"]):
            return "ENERGY"
        else:
            return "OTHER"
    
    def get_todays_observations(self) -> pd.DataFrame:
        """Get all observations for today as DataFrame."""
        with self._lock:
            return pd.DataFrame(self._buffer)
    
    def get_unlabeled_observations(self) -> pd.DataFrame:
        """Get observations that haven't been labeled yet."""
        with self._lock:
            unlabeled = [obs for obs in self._buffer if obs["outcome"] is None]
            return pd.DataFrame(unlabeled)
    
    def export_for_training(self, output_path: str = "data/ml/training_data.parquet"):
        """
        Combine all daily files into one training dataset.
        Only includes labeled observations.
        """
        all_files = list(self.data_dir.glob("observations_*.parquet"))
        
        if not all_files:
            logger.warning("[ML] No observation files found")
            return None
        
        dfs = []
        for f in all_files:
            try:
                df = pd.read_parquet(f)
                # Only include labeled observations
                df = df[df["outcome"].notna()]
                dfs.append(df)
            except Exception as e:
                logger.error(f"[ML] Error reading {f}: {e}")
        
        if not dfs:
            logger.warning("[ML] No labeled observations found")
            return None
        
        combined = pd.concat(dfs, ignore_index=True)
        combined.to_parquet(output_path, index=False)
        
        logger.info(f"[ML] Exported {len(combined)} observations to {output_path}")
        return combined


# Singleton instance
_ml_logger: Optional[MLDataLogger] = None

def get_ml_logger() -> MLDataLogger:
    """Get singleton ML logger instance."""
    global _ml_logger
    if _ml_logger is None:
        _ml_logger = MLDataLogger()
    return _ml_logger
