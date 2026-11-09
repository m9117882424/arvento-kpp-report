-- A single (day, plate) entry cannot retain results for multiple combinations
-- of geofences, speed thresholds and lunch/parking settings. Preserve every
-- verified calculation by its versioned, source-aware SHA-256 digest.
-- Use a new table rather than dropping a constraint from the live cache while
-- GPS readers may hold long transactions. The old table remains as a safe
-- rollback source and is copied once during this transactional migration.
CREATE TABLE IF NOT EXISTS efficiency_gps_metric_variants (
    cache_key TEXT PRIMARY KEY CHECK (cache_key ~ '^[a-f0-9]{64}$'),
    report_day DATE NOT NULL,
    normalized_plate TEXT NOT NULL CHECK (length(normalized_plate) BETWEEN 1 AND 40),
    metrics JSONB NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_efficiency_gps_metric_variants_day_plate
    ON efficiency_gps_metric_variants (report_day, normalized_plate);

CREATE INDEX IF NOT EXISTS ix_efficiency_gps_metric_variants_stored_at
    ON efficiency_gps_metric_variants (stored_at);

INSERT INTO efficiency_gps_metric_variants
    (cache_key, report_day, normalized_plate, metrics, stored_at)
SELECT cache_key, report_day, normalized_plate, metrics, stored_at
FROM efficiency_gps_daily_metrics
ON CONFLICT (cache_key) DO NOTHING;
