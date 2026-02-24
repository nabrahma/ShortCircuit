# Phase 44.5 Open Items (Tracked)

Updated: 2026-02-24

## Open Incident

- `INC-2026-02-23-CODE50-TRADEMANAGER`
  - Status: OPEN
  - Scope: `trade_manager.py` sync execution path
  - Symptom: Fyers `code -50` on order placement for `INDOTECH-EQ`
  - Root cause: Not yet confirmed from broker-rejected payload
  - Immediate mitigation: Added pre-order debug snapshot in `trade_manager.execute_logic()` that logs `entry_data`, `qty`, `ltp`, and `productType` before `place_order`.

- `INC-2026-02-23-HARDSTOP-METHOD-GAP`
  - Status: CONFIRMED + MITIGATED
  - Scope: `focus_engine.py` + `order_manager.py`
  - Finding: Hard-stop detection confirmed non-functional from 2026-02-17 through 2026-02-23. `focus_engine.py` called `monitor_hard_stop_status(symbol)` while method was missing, and broad `try/except Exception` at `focus_engine.py:353` swallowed resulting `AttributeError`.
  - Risk: SL fills in that window were undetected by monitor path.
  - Mitigation:
    - Added `OrderManager.monitor_hard_stop_status(...)`
    - Added unified close cleanup path to release capital and write DB closure on hard-stop detection
    - Added SyncWrapper fail-fast + startup interface assertion to prevent missing-method silent degradation

## Phase 44.5 Risk Items

- `44.5-A`: `_get_health_block()` WebSocket health currently relies on cached flags; add live probe path (target 44.6).
- `44.5-B`: `send_eod_summary()` currently depends on in-memory `_session_trades`; add PostgreSQL fallback via `db.get_today_trades()`.
- `44.5-C`: Keep safety tests aligned with current async `OrderManager` interface and monitor for future API drift.
- `44.5-D`: Hard-stop detection confirmed non-functional Feb 17–23 due to broad `try/except Exception` in focus loop swallowing `AttributeError` on missing `monitor_hard_stop_status`. Startup reconciliation recovered state on each boot.
- `44.5-F`: Broad `try/except Exception` in `focus_engine.py` remains. Startup assertion mitigates boot-time interface mismatch; runtime breakage can still be swallowed. Narrow exception scope in Phase 44.6.
- `44.5-G`: Runtime bridge removal audit tracked in `bridge_audit.txt`. This file must be regenerated after each migration pass until no `run_coroutine_threadsafe`/`SyncWrapper`/`AsyncExecutor` runtime call sites remain.
