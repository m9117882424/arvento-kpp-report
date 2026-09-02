-- Shared versioned geofence store used by the editor and report engines.
CREATE TABLE IF NOT EXISTS geofences (
    id BIGSERIAL PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    geofence_type TEXT NOT NULL,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS geofence_versions (
    id BIGSERIAL PRIMARY KEY,
    geofence_id BIGINT NOT NULL REFERENCES geofences(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    geometry geometry(Geometry, 4326) NOT NULL,
    valid_from TIMESTAMPTZ NOT NULL DEFAULT now(),
    valid_to TIMESTAMPTZ,
    comment TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (geofence_id, version)
);

CREATE INDEX IF NOT EXISTS ix_geofence_versions_geometry
ON geofence_versions USING GIST (geometry);

CREATE UNIQUE INDEX IF NOT EXISTS ux_geofence_versions_current
ON geofence_versions (geofence_id)
WHERE valid_to IS NULL;
