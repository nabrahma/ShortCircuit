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
DYNAMIC_CONFIG_PATH = Path("data/ml/dynamic_config.json")
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
        else:
            if np.random.rand() > 0.4:
                df.at[i, 'outcome'] = "LOSS"
                
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
    tp1_mult = trial.suggest_float("P51_TP1_ATR_MULT", 1.0, 2.5)
    tp2_mult = trial.suggest_float("P51_TP2_ATR_MULT", 2.0, 4.0)
    tp3_mult = trial.suggest_float("P51_TP3_ATR_MULT", 3.0, 6.0)
    
    # 3. Simulate Logic
    # Filter for trades that pass gates
    mask = (
        (df['gain_pct'] >= g1_min_gain) &
        (df['vwap_slope'] <= g4_max_slope) &
        (df['rvol'] >= g7_min_rvol)
    )
    sim_df = df[mask].copy()
    
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
        trial_tp1_pct = tp1_mult * atr_pct
        trial_tp2_pct = tp2_mult * atr_pct
        trial_tp3_pct = tp3_mult * atr_pct
        
        mae = row.get('max_adverse', 100) # MAE is price going AGAINST us
        mfe = row.get('max_favorable', -100) # MFE is price going WITH us
        
        # Virtual Simulation Check
        if mae >= trial_sl_pct:
            trade_pnl = -trial_sl_pct
        elif mfe >= trial_tp3_pct:
            trade_pnl = trial_tp3_pct
            wins += 1
        elif mfe >= trial_tp2_pct:
            trade_pnl = trial_tp2_pct * 0.8 # Weighted exit
            wins += 0.8
        elif mfe >= trial_tp1_pct:
            trade_pnl = trial_tp1_pct * 0.4
            wins += 0.4
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
    study = optuna.create_study(direction="maximize")
    
    logger.info("🧪 Staring Grid Search... Executing 1000 Simulated Realities...")
    study.optimize(lambda t: objective(t, df), n_trials=1000, n_jobs=-1, show_progress_bar=True)
    
    best_params = study.best_params
    best_value = study.best_value
    
    print("\n" + "="*50)
    print("🏆 OPTIMIZATION COMPLETE 🏆")
    print("="*50)
    print(f"Best Simulated Total PnL: {best_value:.2f}%")
    print(f"Optimal Configuration Discovered:")
    for param, val in best_params.items():
        print(f" -> {param}: {val:.3f}")
        
    # Phase 70: Export back to bot!
    logger.info(f"Writing {len(best_params)} dynamic thresholds to {DYNAMIC_CONFIG_PATH}...")
    with open(DYNAMIC_CONFIG_PATH, "w") as f:
        json.dump(best_params, f, indent=4)
        
    print(f"✅ The Bot will load these updated thresholds heavily weighted towards profit on next start.")

if __name__ == "__main__":
    run_optimizer()
