-- Phase 94.2: Add leverage tracking to orders and positions
ALTER TABLE orders ADD COLUMN IF NOT EXISTS leverage NUMERIC(4,2) DEFAULT 5.0;
ALTER TABLE positions ADD COLUMN IF NOT EXISTS leverage NUMERIC(4,2) DEFAULT 5.0;
