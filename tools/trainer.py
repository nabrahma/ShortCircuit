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
    logger.info("Generating synthetic mock trades to verify optimization engine...")
    import numpy as np
    np.random.seed(42)
    n_samples = 200
    
    data = {
        'obs_id': [f"trade_{i}" for i in range(n_samples)],
        'gain_pct': np.random.uniform(5.0, 15.0, n_samples),
        'rvol': np.random.uniform(1.0, 10.0, n_samples),
        'vwap_slope': np.random.uniform(-0.5, 4.5, n_samples),
        'outcome': np.random.choice(["WIN", "LOSS", "BREAKEVEN"], n_samples, p=[0.3, 0.5, 0.2]),
        'pnl_pct': np.random.normal(0, 2.0, n_samples)
    }
    
    # artificially create a mathematical edge in the mock data:
    # trades where gain_pct > 8.0 and vwap_slope < 3.0 win more
    for i in range(n_samples):
        if data['gain_pct'][i] > 8.0 and data['vwap_slope'][i] < 3.0:
            if np.random.rand() > 0.3:  # 70% win rate
                data['outcome'][i] = "WIN"
                data['pnl_pct'][i] = np.random.uniform(2.0, 5.0)
                
    return pd.DataFrame(data)

def objective(trial, df):
    """
    Optuna objective function.
    Finds parameters that maximize net PnL while maintaining a safe win rate.
    """
    # 1. Define the search space
    g1_min_gain = trial.suggest_float("P65_G1_NET_GAIN_THRESHOLD", 5.0, 12.0)
    g4_max_slope = trial.suggest_float("P57_G4_DIVERGENCE_SD", 1.0, 4.0)
    g7_min_rvol = trial.suggest_float("P65_G7_VOLUME_Z_SCORE_THRESHOLD", 1.5, 5.0)
    
    # 2. Simulate the historical trades using these thresholds
    # We only take trades that would have PASSED these new thresholds
    mask = (
        (df['gain_pct'] >= g1_min_gain) &
        (df['vwap_slope'] <= g4_max_slope) &
        (df['rvol'] >= g7_min_rvol)
    )
    
    simulated_trades = df[mask]
    
    # 3. Handle edge cases efficiently
    num_trades = len(simulated_trades)
    if num_trades < 10:
        # Penalize sets that pass too few trades (over-fitting)
        return -1000.0
        
    total_pnl = simulated_trades['pnl_pct'].sum()
    
    # Avoid setups that just pass random trades resulting in massive drawdowns
    win_rate = (simulated_trades['outcome'] == 'WIN').mean()
    if win_rate < 0.35: 
        return -500.0  # Hard penalty
        
    return total_pnl

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
