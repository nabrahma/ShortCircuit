-- ShortCircuit Phase 42.1 - PostgreSQL Migration
-- HFT-Grade Schema for 99.999% Reliability

BEGIN;

-- Enable UUID extension
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- We will use standard timestamps. TimescaleDB can be enabled later if needed for high-volume tick data.
-- For now, we focus on relational integrity.

-- ============================================================
-- ORDERS TABLE (State Machine Persistence)
-- ============================================================
CREATE TABLE IF NOT EXISTS orders (
    order_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    exchange_order_id VARCHAR(50) UNIQUE,
    symbol VARCHAR(20) NOT NULL,
    side VARCHAR(4) NOT NULL CHECK (side IN ('BUY', 'SELL')),
    order_type VARCHAR(20) NOT NULL CHECK (order_type IN ('MARKET', 'LIMIT', 'SL', 'SL-M')), -- SL-M matches Fyers
    qty INTEGER NOT NULL CHECK (qty > 0),
    price DECIMAL(12, 2),
    trigger_price DECIMAL(12, 2),
    
    -- Extended State Machine
    state VARCHAR(30) NOT NULL CHECK (state IN (
        'PENDING',                -- Internal: Created
        'SUBMITTED',              -- Sent to Broker
        'SUBMITTED_UNCONFIRMED',  -- Network sent, no ACK
        'OPEN',                   -- Broker Confirmed
        'PARTIAL_FILL',           -- Partially Filled
        'FILLED',                 -- Fully Filled
        'REJECTED',               -- Broker Rejected
        'CANCELLED',              -- Cancelled
        'CANCEL_PENDING',         -- Cancel sent
        'MODIFY_PENDING',         -- Modify sent
        'EXPIRED',                -- TIF Expired
        'DISCONNECTED'            -- State unknown due to connection loss
    )),
    
    -- Timestamps
    created_at TIMESTAMPTZ DEFAULT NOW(),
    submitted_at TIMESTAMPTZ,
    filled_at TIMESTAMPTZ,
    cancelled_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    
    -- Fill Details
    filled_qty INTEGER DEFAULT 0,
    avg_filled_price DECIMAL(12, 2),
    commission DECIMAL(10, 2),
    
    -- Error Handling
    error_code VARCHAR(50),
    error_message TEXT,
    
    -- Metadata
    signal_id VARCHAR(50),
    strategy_name VARCHAR(50) DEFAULT 'SHORT_CIRCUIT',
    session_date DATE NOT NULL,
    
    -- Audit
    created_by VARCHAR(50) DEFAULT 'BOT'
);

CREATE INDEX IF NOT EXISTS idx_orders_symbol_state ON orders(symbol, state);
CREATE INDEX IF NOT EXISTS idx_orders_session ON orders(session_date);
CREATE INDEX IF NOT EXISTS idx_orders_exchange_id ON orders(exchange_order_id);

-- ============================================================
-- POSITIONS TABLE (Source of Truth)
-- ============================================================
CREATE TABLE IF NOT EXISTS positions (
    position_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    symbol VARCHAR(20) NOT NULL,
    
    -- Quantities (Positive=Long, Negative=Short)
    qty INTEGER NOT NULL,
    entry_price DECIMAL(12, 2) NOT NULL,
    current_price DECIMAL(12, 2),
    
    -- PnL
    unrealized_pnl DECIMAL(12, 2),
    realized_pnl DECIMAL(12, 2) DEFAULT 0,
    
    -- Lifecycle
    state VARCHAR(20) NOT NULL CHECK (state IN (
        'OPEN',
        'CLOSED',
        'ORPHANED',   -- Broker has it, we didn't track it
        'RECONCILED'  -- Fixed via reconciliation
    )),
    
    -- Links
    entry_order_id UUID REFERENCES orders(order_id),
    exit_order_id UUID REFERENCES orders(order_id),
    sl_order_id VARCHAR(50), -- Broker Order ID for Hard Stop
    
    -- Timestamps
    opened_at TIMESTAMPTZ DEFAULT NOW(),
    closed_at TIMESTAMPTZ,
    last_reconciled_at TIMESTAMPTZ,
    
    -- Source
    source VARCHAR(30) NOT NULL CHECK (source IN (
        'SIGNAL',
        'MANUAL',
        'ORPHAN_RECOVERY',
        'RECONCILIATION'
    )),
    
    -- Metadata
    session_date DATE NOT NULL,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_positions_symbol_state ON positions(symbol, state);
CREATE INDEX IF NOT EXISTS idx_positions_open ON positions(state) WHERE state = 'OPEN';


-- ============================================================
-- RECONCILIATION LOG (Audit Trail)
-- ============================================================
CREATE TABLE IF NOT EXISTS reconciliation_log (
    recon_id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    timestamp TIMESTAMPTZ DEFAULT NOW(),
    
    internal_pos_count INTEGER NOT NULL,
    broker_pos_count INTEGER NOT NULL,
    
    -- Discrepancies (JSON for flexibility)
    orphans_detected JSONB,
    phantoms_detected JSONB,
    mismatches JSONB,
    
    status VARCHAR(40) NOT NULL CHECK (status IN (
        'CLEAN',
        'DIVERGENCE_DETECTED',
        'AUTO_RESOLVED',
        'MANUAL_INTERVENTION_REQUIRED'
    )),
    
    resolution_action TEXT,
    check_duration_ms INTEGER,
    
    session_date DATE NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recon_session ON reconciliation_log(session_date);
CREATE INDEX IF NOT EXISTS idx_recon_status ON reconciliation_log(status) WHERE status != 'CLEAN';

-- Trigger for updated_at
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_orders_timestamp
    BEFORE UPDATE ON orders
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at();

COMMIT;
