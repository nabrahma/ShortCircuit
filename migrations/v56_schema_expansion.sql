-- Phase 56: Schema Expansion for Audit Trail
-- Widen VARCHAR columns to prevent StringDataRightTruncationError

BEGIN;

ALTER TABLE gate_results ALTER COLUMN g6_value TYPE VARCHAR(100);
ALTER TABLE gate_results ALTER COLUMN g7_value TYPE VARCHAR(100);
ALTER TABLE gate_results ALTER COLUMN g11_value TYPE VARCHAR(100);
ALTER TABLE gate_results ALTER COLUMN verdict TYPE VARCHAR(50);
ALTER TABLE gate_results ALTER COLUMN first_fail_gate TYPE VARCHAR(100);
ALTER TABLE gate_results ALTER COLUMN data_tier TYPE VARCHAR(50);

-- Phase 66: Session Hardening Fixes
ALTER TABLE orders ALTER COLUMN side TYPE VARCHAR(10);
ALTER TABLE orders DROP CONSTRAINT IF EXISTS orders_side_check;
ALTER TABLE orders ADD CONSTRAINT orders_side_check CHECK (side IN ('BUY', 'SELL', 'SHORT', 'LONG'));

ALTER TABLE positions ALTER COLUMN source TYPE VARCHAR(50);

COMMIT;

-- Verify
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'gate_results'
  AND column_name IN ('g6_value', 'g7_value', 'g11_value', 'verdict', 'first_fail_gate', 'data_tier')
ORDER BY ordinal_position;
