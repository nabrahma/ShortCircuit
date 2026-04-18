#!/usr/bin/env python3
"""
Phase 70: The Weekend Retuner (ML Trainer)
Uses Optuna to natively search for the mathematical edge in trading gate parameters.
"""

import sys
import json
from pathlib import Path
import pandas as pd
import logging
import datetime

try:
    import optuna
except ImportError:
    print("❌ Optuna is not installed. Please run: pip install optuna")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(message)s")
logger = logging.getLogger("Trainer")

# Where dynamic config will be exported
def get_dynamic_config_path(direction: str) -> Path:
    return Path(f"data/ml/dynamic_config_{direction}.json")
TRAINING_DATA_PATH = Path("data/ml")

def load_historical_data() -> pd.DataFrame:
    """Combines all daily ML parquet files into a massive DataFrame."""
    all_files = list(TRAINING_DATA_PATH.glob("observations_*.parquet"))
    
    if not all_files:
        logger.warning("No ML observation files found in data/ml/")
        return pd.DataFrame()
    
    dfs = []
    for f in all_files:
        try:
            df = pd.read_parquet(f)
            # We strictly need labeled outcomes
            df = df[df["outcome"].notna()]
            dfs.append(df)
        except Exception as e:
            logger.error(f"Error reading {f}: {e}")
            
    if not dfs:
        return pd.DataFrame()
        
    combined = pd.concat(dfs, ignore_index=True)
    logger.info(f"✅ Loaded {len(combined)} historically labeled trades for optimization.")
    return combined

def generate_mock_data():
    """Generates synthetic data for testing the optimizer logic internally."""
    logger.info("Generating synthetic mock trades with MFE/MAE for optimization engine...")
    import numpy as np
    np.random.seed(42)
    n_samples = 300
    
    data = {
        'obs_id': [f"trade_{i}" for i in range(n_samples)],
        'gain_pct': np.random.uniform(5.0, 15.0, n_samples),
        'rvol': np.random.uniform(1.0, 10.0, n_samples),
        'vwap_slope': np.random.uniform(-0.5, 4.5, n_samples),
        'ltp': np.random.uniform(100, 2000, n_samples),
        'outcome': [None] * n_samples,
        'max_adverse': np.random.uniform(0.1, 2.0, n_samples),
        'max_favorable': np.random.uniform(0.1, 5.0, n_samples),
    }
    
    df = pd.DataFrame(data)
    df['atr'] = df['ltp'] * 0.012  # Assume 1.2% ATR
    
    # Create an edge: Trades with high gain and high Rvol tend to trend further (higher MFE)
    for i in range(n_samples):
        if df.at[i, 'gain_pct'] > 9.0 and df.at[i, 'rvol'] > 5.0:
            df.at[i, 'max_favorable'] = np.random.uniform(2.0, 6.0)
            df.at[i, 'max_adverse'] = np.random.uniform(0.1, 0.4)
            df.at[i, 'outcome'] = "WIN" 
            df.at[i, 'outcome'] = "WIN" 
        else:
            if np.random.rand() > 0.4:
                df.at[i, 'outcome'] = "LOSS"
                
    # Phase 94 simulation additions
    df["direction"] = np.random.choice(["SHORT", "LONG"], n_samples)
    return df

def objective(trial, df):
    """
    Optuna objective function with Virtual Path Simulation.
    Optimizes both ENTRY gates and EXIT risk parameters.
    """
    # 1. Define Entry Gate Search Space
    g1_min_gain = trial.suggest_float("P65_G1_NET_GAIN_THRESHOLD", 5.0, 12.0)
    g4_max_slope = trial.suggest_float("P57_G4_DIVERGENCE_SD", 1.0, 4.0)
    g7_min_rvol = trial.suggest_float("P65_G7_VOLUME_Z_SCORE_THRESHOLD", 1.5, 6.0)
    
    # 2. Define Exit Multiplier Search Space (ATR Multipliers)
    sl_mult = trial.suggest_float("P51_SL_ATR_MULTIPLIER", 0.3, 0.8)
    tp_mult = trial.suggest_float("P78_SINGLE_TP_ATR_MULT_DEFAULT", 0.8, 2.5)
    
    # 3. Simulate Logic
    # Apply direction-aware vwap filters (momentum checking)
    sim_df = df.copy()
    
    # G1 and G7 apply generally
    mask_g1_g7 = (sim_df['gain_pct'] >= g1_min_gain) & (sim_df['rvol'] >= g7_min_rvol)
    
    # G4 applies differently per direction
    # SHORT: want slope <= vwap threshold (dropping or flat)
    # LONG: want slope >= vwap threshold (rising or flat)
    # For optuna let's assume we optimize an absolute slope threshold value.
    sim_df['vwap_slope_abs'] = sim_df['vwap_slope'].abs()
    mask_g4 = sim_df['vwap_slope_abs'] <= g4_max_slope
    
    sim_df = sim_df[mask_g1_g7 & mask_g4]
    
    if len(sim_df) < 10:
        return -2000.0  # Overfitting penalty
        
    total_sim_pnl = 0.0
    wins = 0
    
    for _, row in sim_df.iterrows():
        entry = row['ltp']
        atr = row.get('atr', entry * 0.01) # fallback to 1%
        if atr <= 0: atr = entry * 0.01
        
        # Convert ATR multipliers to price distance percentage
        atr_pct = (atr / entry) * 100
        
        trial_sl_pct = sl_mult * atr_pct
        trial_tp_pct = tp_mult * atr_pct
        
        # Override for low gain
        if row['gain_pct'] < 9.0:
            trial_tp_pct = 0.5 * atr_pct

        mae = row.get('max_adverse', 100) # MAE is price going AGAINST us
        mfe = row.get('max_favorable', -100) # MFE is price going WITH us
        
        # Virtual Simulation Check
        if mae >= trial_sl_pct:
            trade_pnl = -trial_sl_pct
        elif mfe >= trial_tp_pct:
            trade_pnl = trial_tp_pct
            wins += 1
        else:
            trade_pnl = mfe * 0.1
            
        total_sim_pnl += trade_pnl
        
    win_rate = wins / len(sim_df)
    if win_rate < 0.40:
        return -1000.0 # Safety penalty
        
    return total_sim_pnl

def run_optimizer():
    df = load_historical_data()
    if df.empty:
        logger.warning("No production data. Running with mock data to demonstrate capability...")
        df = generate_mock_data()
        
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    
    for direction in ["SHORT", "LONG"]:
        direction_df = df[df.get("direction", "SHORT") == direction]
        
        if len(direction_df) < 10:
            logger.warning(f"Not enough data to optimize for {direction} ({len(direction_df)} samples). Skipping.")
            continue
            
        study = optuna.create_study(direction="maximize")
        
        logger.info(f"\n🧪 Staring Grid Search for {direction}... Executing 1000 Simulated Realities...")
        study.optimize(lambda t: objective(t, direction_df), n_trials=1000, n_jobs=-1, show_progress_bar=True)
        
        best_params = study.best_params
        best_value = study.best_value
        
        print("\n" + "="*50)
        print(f"🏆 {direction} OPTIMIZATION COMPLETE 🏆")
        print("="*50)
        print(f"Best Simulated Total PnL: {best_value:.2f}%")
        print(f"Optimal Configuration Discovered:")
        for param, val in best_params.items():
            print(f" -> {param}: {val:.3f}")
            
        # Phase 70: Export back to bot!
        out_path = get_dynamic_config_path(direction)
        logger.info(f"Writing {len(best_params)} dynamic thresholds to {out_path}...")
        with open(out_path, "w") as f:
            json.dump(best_params, f, indent=4)
            
        print(f"✅ The Bot will load {direction} thresholds heavily weighted towards profit on next start.")

if __name__ == "__main__":
    run_optimizer()
