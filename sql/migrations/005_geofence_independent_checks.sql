-- A geofence has one physical purpose and multiple independent report checks.
-- Existing lists and geometry version history remain intact.
ALTER TABLE geofences
    ADD COLUMN IF NOT EXISTS parking_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS allowed_area_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS speed_rule TEXT NOT NULL DEFAULT 'inherit',
    ADD COLUMN IF NOT EXISTS speed_threshold_kmh DOUBLE PRECISION;

-- Preserve previously automatic tunnel exclusions exactly as before.
UPDATE geofences
SET speed_rule = 'exclude'
WHERE upper(geofence_type) IN ('SPEED_EXCLUSION', 'TUNNEL')
  AND speed_rule = 'inherit';

ALTER TABLE geofences
    ADD CONSTRAINT geofences_speed_rule_valid
        CHECK (speed_rule IN ('inherit', 'exclude', 'limit')),
    ADD CONSTRAINT geofences_speed_threshold_valid
        CHECK (
            (speed_rule = 'limit' AND speed_threshold_kmh IS NOT NULL
                AND speed_threshold_kmh BETWEEN 5 AND 250)
            OR (speed_rule <> 'limit' AND speed_threshold_kmh IS NULL)
        );
