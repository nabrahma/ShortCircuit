-- migrations/v44_8_3_gate_results_g9_type_fix.sql
-- PRD-3 R2: Fix g9_value column type mismatch.
-- Root cause: GateResult.g9_value is Optional[str] (HTF confluence reason text)
-- but column was declared NUMERIC(8,4) — causing decimal.ConversionSyntax on every flush.

BEGIN;

-- Verify current type before altering
DO $$
DECLARE
    col_type TEXT;
BEGIN
    SELECT data_type INTO col_type
    FROM information_schema.columns
    WHERE table_name = 'gate_results' AND column_name = 'g9_value';

    IF col_type = 'numeric' THEN
        RAISE NOTICE 'g9_value is NUMERIC — applying fix';
    ELSIF col_type = 'character varying' THEN
        RAISE NOTICE 'g9_value is already VARCHAR — no action needed';
    ELSE
        RAISE NOTICE 'g9_value type is: % — review before proceeding', col_type;
    END IF;
END $$;

-- Fix: convert NUMERIC g9_value → VARCHAR(200)
ALTER TABLE gate_results
    ALTER COLUMN g9_value TYPE VARCHAR(200)
    USING g9_value::TEXT;

-- Also fix g11_value: schema says VARCHAR(20) but it holds floats in some paths
-- Widen to be safe:
ALTER TABLE gate_results
    ALTER COLUMN g11_value TYPE VARCHAR(50)
    USING g11_value::TEXT;

COMMIT;

-- Verify
SELECT column_name, data_type, character_maximum_length
FROM information_schema.columns
WHERE table_name = 'gate_results'
  AND column_name IN ('g9_value', 'g11_value')
ORDER BY ordinal_position;
-- Expected: g9_value → character varying(200), g11_value → character varying(50)
