-- PRD-008: Gate Result Audit Trail
-- Migration: v44_8_2_gate_results.sql
-- Run: python apply_migration.py migrations/v44_8_2_gate_results.sql

CREATE TABLE IF NOT EXISTS gate_results (
    id              BIGSERIAL PRIMARY KEY,
    session_date    DATE NOT NULL,
    scan_id         INTEGER NOT NULL,
    evaluated_at    TIMESTAMPTZ NOT NULL,
    symbol          VARCHAR(30) NOT NULL,

    -- Market context
    nifty_regime    VARCHAR(10),
    nifty_level     NUMERIC(10,2),

    -- Gate results (NULL=not evaluated, TRUE=pass, FALSE=fail)
    -- Analyzer gates G1-G8
    g1_pass     BOOLEAN,  g1_value  NUMERIC(8,4),   -- gain range check
    g2_pass     BOOLEAN,  g2_value  NUMERIC(8,4),   -- RVOL validity
    g3_pass     BOOLEAN,  g3_value  NUMERIC(8,4),   -- circuit guard
    g4_pass     BOOLEAN,  g4_value  NUMERIC(8,4),   -- momentum check
    g5_pass     BOOLEAN,  g5_value  NUMERIC(8,4),   -- exhaustion at stretch
    g6_pass     BOOLEAN,  g6_value  VARCHAR(20),    -- pro confluence / POC
    g7_pass     BOOLEAN,  g7_value  VARCHAR(20),    -- market regime
    g8_pass     BOOLEAN,  g8_value  NUMERIC(8,4),   -- signal manager (daily limit)

    -- Focus engine gates G9-G12
    g9_pass     BOOLEAN,  g9_value  NUMERIC(8,4),   -- cooldown check
    g10_pass    BOOLEAN,  g10_value NUMERIC(10,2),  -- capital availability
    g11_pass    BOOLEAN,  g11_value VARCHAR(20),    -- order pre-flight
    g12_pass    BOOLEAN,  g12_value NUMERIC(8,4),   -- final conviction

    -- Outcome
    verdict         VARCHAR(20) NOT NULL,  -- SIGNAL_FIRED | REJECTED | DATA_ERROR | SUPPRESSED
    first_fail_gate VARCHAR(30),
    rejection_reason TEXT,
    data_tier       VARCHAR(20),           -- WS_CACHE | HYBRID | REST_EMERGENCY

    -- If signal fired
    entry_price     NUMERIC(10,2),
    qty             INTEGER
);

-- Indexes for post-session analysis
CREATE INDEX IF NOT EXISTS idx_gate_date_symbol
    ON gate_results(session_date, symbol);

CREATE INDEX IF NOT EXISTS idx_gate_verdict
    ON gate_results(session_date, verdict);

CREATE INDEX IF NOT EXISTS idx_gate_first_fail
    ON gate_results(session_date, first_fail_gate)
    WHERE first_fail_gate IS NOT NULL;

-- Useful query templates (as comments for reference):
--
-- Which gate is blocking a symbol most this week?
-- SELECT first_fail_gate, COUNT(*) as rejections
-- FROM gate_results
-- WHERE symbol = 'NSE:JINDRILL-EQ'
--   AND session_date >= CURRENT_DATE - 7
-- GROUP BY first_fail_gate
-- ORDER BY rejections DESC;
--
-- What data tier was used each scan?
-- SELECT scan_id, data_tier, COUNT(*) as candidates
-- FROM gate_results
-- WHERE session_date = CURRENT_DATE
-- GROUP BY scan_id, data_tier ORDER BY scan_id;
