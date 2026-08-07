# Data model

Three storage layers, chosen for different access patterns
([ADR-004](DECISIONS.md), [ADR-005](DECISIONS.md)):

| Layer | Holds | Access pattern |
|---|---|---|
| PostgreSQL | Orders, positions, reconciliation events, gate results | Transactional, queried by key, must be correct at every instant |
| Parquet + CSV | ML feature observations | Append-only, columnar, read in bulk at analysis time |
| Flat files | Session logs, signal CSV, EOD reports | Append-only, human-readable, grep-able |

Schema is defined by the files in [`migrations/`](../migrations/), applied in
filename order.

---

## PostgreSQL

### `orders`

Every order the system submits, with a state machine detailed enough to
distinguish "we do not know" from "it failed".

| Column | Type | Notes |
|---|---|---|
| `order_id` | UUID PK | Internal identifier |
| `exchange_order_id` | VARCHAR UNIQUE | Broker's id — the UNIQUE constraint is what makes entry logging idempotent under retry |
| `symbol` | VARCHAR | `NSE:SYMBOL-EQ` |
| `side` | VARCHAR | `BUY` / `SELL` |
| `order_type` | VARCHAR | `MARKET`, `LIMIT`, `SL`, `SL-M` |
| `qty` | INTEGER | Positive |
| `price`, `trigger_price` | DECIMAL(12,2) | |
| `state` | VARCHAR | See below |
| `leverage` | NUMERIC(4,2) | Added v44.9.0 — records 5x or the 4x fallback |
| `session_date` | DATE | Partition key for daily analysis |
| `created_by` | VARCHAR | `BOT`, or a recovery source |

**Order states.** The interesting ones are the uncertainty states, which exist
because a network failure is not the same as a rejection:

```
PENDING → SUBMITTED → SUBMITTED_UNCONFIRMED → OPEN → PARTIAL_FILL → FILLED
                                            ↘ REJECTED / CANCELLED / EXPIRED
                                            ↘ DISCONNECTED
```

`SUBMITTED_UNCONFIRMED` means the request was sent but no acknowledgement
arrived. `DISCONNECTED` means the state is genuinely unknown. Both require
reconciliation against the broker rather than an assumption — the failure mode
described in [D-005](DISCOVERIES.md).

### `positions`

| Column | Type | Notes |
|---|---|---|
| `position_id` | UUID PK | |
| `symbol` | VARCHAR | |
| `qty` | INTEGER | Stored as a **positive magnitude**; direction lives in the order side. The broker reports shorts as negative `netQty`, so reconciliation compares absolute values — mixing the two produced a spurious mismatch on every short |
| `entry_price`, `current_price` | DECIMAL(12,2) | |
| `unrealized_pnl`, `realized_pnl` | DECIMAL(12,2) | Internal risk tracking only; never published |
| `state` | VARCHAR | `OPEN`, `CLOSED`, `ORPHANED`, `RECONCILED` |
| `leverage` | NUMERIC(4,2) | |
| `session_date` | DATE | |
| `opened_at`, `closed_at` | TIMESTAMP | |

`ORPHANED` and `RECONCILED` are first-class states, not error flags — the system
expects divergence to happen and records how it was resolved.

### `reconciliation_log`

One row per detected divergence.

| Column | Notes |
|---|---|
| `timestamp` | |
| `internal_position_count` / `broker_position_count` | The two views being compared |
| `orphaned_positions` | JSON array |
| `phantom_positions` | JSON array |
| `quantity_mismatches` | JSON array |
| `status` | e.g. `DIVERGENCE_DETECTED` |
| `check_duration_ms` | Cycle cost |
| `session_date` | |

This table is the audit trail for the claim that reconciliation works. Its row
count over time is a publishable system metric.

### `gate_results`

**Every candidate that reaches analyzer evaluation produces a row**, whether it
becomes a signal or not. This is what makes the repository a research instrument
rather than only an execution engine.

| Column | Notes |
|---|---|
| `symbol`, `scan_id`, `session_date` | Correlates a rejection to the scan cycle that produced it |
| `data_tier` | `WS_CACHE`, `HYBRID`, or `REST_EMERGENCY` — records the data quality the decision was made on |
| `g2_pass` … `g12_pass` | Per-gate outcome |
| `g5_value`, `g6_value`, `g7_value`, `g9_value` | The measured value at each gate |
| `verdict` | `ANALYZER_PASS`, `REJECTED`, `SIGNAL_FIRED`, `SUPPRESSED`, `OBSERVED_NO_CAPITAL`, `DATA_ERROR` |
| `first_fail_gate` | Where it stopped — the field the rejection breakdown aggregates |
| `rejection_reason` | Human-readable |
| `entry_price`, `qty` | Populated only when the verdict is `SIGNAL_FIRED` |

`OBSERVED_NO_CAPITAL` matters: it records a signal that passed every gate but was
not taken because the single capital slot was occupied. That makes the cost of
[ADR-002](DECISIONS.md) measurable rather than invisible.

---

## Parquet observations

Written to `data/ml/observations_YYYY-MM-DD.parquet`, with a CSV mirror for
inspection. One row per signal, written at signal time; outcome fields are filled
at close or at end of day.

**Feature fields** (at signal time): identifiers and timestamps; price context
(`ltp`, `prev_close`, `day_high`, `day_low`, `gain_pct`); VWAP features (`vwap`,
`vwap_distance_pct`, `vwap_sd`, `vwap_slope`); volume (`volume_current`,
`volume_avg_20`, `rvol`); structure (`pattern`, `candle_body_pct`,
`upper_wick_pct`, `lower_wick_pct`); strategy quality (`stretch_score`,
`vol_fade_ratio`, `confidence`, `pattern_bonus`); context (`nifty_trend`,
`sector`, `time_bucket`, `direction`); and risk parameters (`atr`, `sl_price`,
`tp_price`, `leverage`).

**Label fields** (filled later): `outcome`, `exit_price`, `max_favorable`,
`max_adverse`, `pnl_pct`, `hold_time_mins`, `label_source` (`LIVE` or `GHOST`),
`exit_reason`.

`label_source` distinguishes a real executed outcome from a simulated one
produced by the EOD missed-signal audit. Mixing the two silently would corrupt
any model trained on the result.

**A note on data quality.** Four of the strategy quality fields —
`stretch_score`, `vol_fade_ratio`, `confidence`, `pattern_bonus` — were declared
in the schema and passed by the analyzer but never written, leaving them null in
every observation before 2026-08-06. Historical rows cannot be recovered. See
[KNOWN_GAPS.md](KNOWN_GAPS.md).

---

## Flat files

| Path | Contents |
|---|---|
| `logs/YYYY-MM-DD_session.log` | Full session log, structured tags (`[ENTRY]`, `[EXIT]`, `[GATE]`, `[RECONCILE]`) |
| `logs/signals.csv` | Human-readable signal log |
| `logs/rejections_YYYYMMDD.log` | Daily per-gate rejection summary |
| `reports/session_analysis_*.md` | Generated session report |
| `data/access_token.txt` | Cached broker token — git-ignored |

All are git-ignored. The structured tags are parsed by
`tools/analyze_session_log.py`, so changing them breaks that tool.

---

## Applying migrations

```bash
for f in migrations/*.sql; do
  psql -h "$DB_HOST" -U "$DB_USER" -d "$DB_NAME" -f "$f"
done
```

Applied in filename order. Every migration is written to be re-runnable
(`CREATE TABLE IF NOT EXISTS`, `ADD COLUMN IF NOT EXISTS`), so re-applying the
directory is safe. The CI integration job does exactly this against a clean
PostgreSQL 16 service on every push.
