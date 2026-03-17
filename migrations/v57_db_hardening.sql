-- Database Hardening: v57_db_hardening.sql
-- Goal: Widen all VARCHAR columns that could potentially cause "value too long" crashes.
-- Run: python run_migration.py

BEGIN;

-- 1. Table: orders
ALTER TABLE orders ALTER COLUMN symbol TYPE VARCHAR(50);
ALTER TABLE orders ALTER COLUMN side TYPE VARCHAR(20); -- Already was 10, now 20
ALTER TABLE orders ALTER COLUMN order_type TYPE VARCHAR(50);
ALTER TABLE orders ALTER COLUMN state TYPE VARCHAR(50);
ALTER TABLE orders ALTER COLUMN created_by TYPE VARCHAR(100);
ALTER TABLE orders ALTER COLUMN exchange_order_id TYPE VARCHAR(100);

-- 2. Table: positions
ALTER TABLE positions ALTER COLUMN symbol TYPE VARCHAR(50);
ALTER TABLE positions ALTER COLUMN state TYPE VARCHAR(50);
-- DROP and ADD constraint for state if it exists
DO $$ BEGIN
    ALTER TABLE positions DROP CONSTRAINT IF EXISTS positions_state_check;
EXCEPTION
    WHEN undefined_object THEN NULL;
END $$;
-- We allow any reasonable state string now to prevent crashes, but keep standard ones.
ALTER TABLE positions ADD CONSTRAINT positions_state_check CHECK (state IN ('OPEN', 'CLOSED', 'ORPHANED', 'RECONCILED', 'ERROR', 'MANUAL_INTERVENTION_REQUIRED'));

-- 3. Table: gate_results (PRD-008 Audit Trail)
ALTER TABLE gate_results ALTER COLUMN symbol TYPE VARCHAR(50);
ALTER TABLE gate_results ALTER COLUMN nifty_regime TYPE VARCHAR(50);
ALTER TABLE gate_results ALTER COLUMN g6_value TYPE VARCHAR(250);
ALTER TABLE gate_results ALTER COLUMN g7_value TYPE VARCHAR(250);
ALTER TABLE gate_results ALTER COLUMN g9_value TYPE VARCHAR(250);
ALTER TABLE gate_results ALTER COLUMN g11_value TYPE VARCHAR(250);
ALTER TABLE gate_results ALTER COLUMN verdict TYPE VARCHAR(100);
ALTER TABLE gate_results ALTER COLUMN first_fail_gate TYPE VARCHAR(150);
ALTER TABLE gate_results ALTER COLUMN data_tier TYPE VARCHAR(100);

-- 4. Table: reconciliation_log
ALTER TABLE reconciliation_log ALTER COLUMN status TYPE VARCHAR(100);

COMMIT;

-- Verify
SELECT table_name, column_name, data_type, character_maximum_length
FROM information_schema.columns 
WHERE table_name IN ('orders', 'positions', 'gate_results', 'reconciliation_log')
  AND data_type = 'character varying'
ORDER BY table_name, ordinal_position;
