-- Track every input that can make a prepared consolidated day stale.
-- Existing installations already have this table; fresh installations receive
-- the same columns from consolidated_cache.ensure_schema when first used.
DO $migration$
BEGIN
    IF to_regclass('public.consolidated_cache_days') IS NOT NULL THEN
        ALTER TABLE consolidated_cache_days
            ADD COLUMN IF NOT EXISTS gps_max_received_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS distance_max_fetched_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS roster_loaded_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS geofence_updated_at TIMESTAMPTZ,
            ADD COLUMN IF NOT EXISTS calculation_version TEXT,
            ADD COLUMN IF NOT EXISTS source_vehicle_count INTEGER NOT NULL DEFAULT 0;
    END IF;

    IF to_regclass('public.consolidated_report_cache') IS NOT NULL THEN
        ALTER TABLE consolidated_report_cache
            ALTER COLUMN worked_hours DROP NOT NULL;
    END IF;
END
$migration$;
