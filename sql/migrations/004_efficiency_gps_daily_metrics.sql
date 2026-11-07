-- Persist only verified historical, per-vehicle/day GPS aggregates. The
-- SHA-256 key incorporates source watermarks, metric algorithm revision,
-- selected geofences/versions and user-adjustable calculation settings.
-- A changed dependency gets a new key; no existing row is treated as fresh.
CREATE TABLE IF NOT EXISTS efficiency_gps_daily_metrics (
    cache_key TEXT PRIMARY KEY CHECK (cache_key ~ '^[a-f0-9]{64}$'),
    report_day DATE NOT NULL,
    normalized_plate TEXT NOT NULL CHECK (length(normalized_plate) BETWEEN 1 AND 40),
    metrics JSONB NOT NULL CHECK (jsonb_typeof(metrics) = 'object'),
    stored_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS efficiency_gps_daily_metrics_day_plate_idx
    ON efficiency_gps_daily_metrics(report_day, normalized_plate);
