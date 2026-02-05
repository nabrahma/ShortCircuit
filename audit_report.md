# 🕵️ Senior Codebase Audit Report
**Date:** 2026-02-05
**Auditor:** AntiGravity (Senior Agent)
**Project:** ShortCircuit (Fyers Edition)

## 🚨 Executive Summary
The codebase is structured professionally with clear separation of concerns (`Scanner` -> `Analyzer` -> `TradeManager`). The Phase 27 "Institutional" logic (OI, dPOC) is correctly integrated. However, we identified **one critical code bug** (Dead Code) and **one major performance bottleneck** (API efficiency). The bot is "safe" but "expensive" on API limits.

## 🔍 Critical Vulnerabilities (Severity: HIGH)
### 1. Unreachable Code in `trade_manager.py` (Dead Code)
-   **Location:** `trade_manager.py:112`
-   **Issue:** Double `return` statement in the `execute_logic` success block.
-   **Impact:** Zero runtime impact (Python ignores the second return), but it indicates copy-paste error and confusing maintainability.
-   **Fix:** Delete lines 112-120.

## ⚠️ Major Issues (Severity: MEDIUM)
### 1. API Rate Limit Risk (Scanner Efficiency)
-   **Location:** `scanner.py:173` inside `scan_market` loop.
-   **Issue:** For every candidate passing the Gain filter, we call:
    1.  `check_chart_quality` -> `fyers.history` (1 call)
    2.  `check_setup` -> `_check_circuit_guard` -> `fyers.depth` (1 call)
    3.  `check_setup` -> `tape_reader` -> `fyers.depth` (1 call - redundant?)
-   **Impact:** If 50 stocks pass the initial filter, we generate **150 extra API calls** instantly. Fyers limit is ~10 calls/sec. This risks `429 Too Many Requests`.
-   **Mitigation:** 
    -   Cache Depth Data (Circuit Guard and Tape Reader use same data).
    -   Optimize `check_chart_quality` to run *after* other checks if possible, or use `quotes` volume data more aggressively.

### 2. CSV Download Dependency
-   **Location:** `scanner.py:24`
-   **Issue:** Downloads `https://public.fyers.in/sym_details/NSE_CM.csv` on every cold start.
-   **Risk:** If Fyers Public CDN is down, the bot fails to start (`fetch_nse_symbols` returns empty).
-   **Fix:** Cache the CSV locally (`data/nse_cm.csv`) and only refresh if > 24 hours old.

## 💡 Minor Improvements (Severity: LOW)
### 1. Hardcoded Thresholds
-   **Location:** `scanner.py` (6-18%), `analyzer.py` (1.5% Circuit).
-   **Suggestion:** Move these to `config.py` for easier tuning (e.g., `GAIN_FILTER_MIN`, `CIRCUIT_BUFFER`).

### 2. TPO Calculation Speed
-   **Location:** `market_profile.py`
-   **Issue:** Custom Python loop for Value Area calculation.
-   **Fix:** Low priority, but could be vectorized with `numpy` for 10x speedup if backtesting large datasets.

## ✅ Recommendations
1.  **Immediate:** Fix the `trade_manager.py` double return.
2.  **Short Term:** Implement a simple "Cache" for Depth calls in `analyzer` to avoid double-fetching for Circuit Guard and Tape Reader.
3.  **Long Term:** Move Filter constants to `config.py`.
