CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS vehicles (
    id BIGSERIAL PRIMARY KEY,
    device_no TEXT UNIQUE,
    plate TEXT NOT NULL,
    normalized_plate TEXT NOT NULL,
    driver TEXT,
    group_name TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    first_seen_at TIMESTAMPTZ,
    last_seen_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_vehicles_normalized_plate ON vehicles(normalized_plate);

CREATE TABLE IF NOT EXISTS gps_points (
    id BIGSERIAL NOT NULL,
    device_no TEXT NOT NULL,
    plate TEXT NOT NULL,
    normalized_plate TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    position geography(Point, 4326) NOT NULL,
    speed_kmh DOUBLE PRECISION,
    distance_km DOUBLE PRECISION,
    address TEXT,
    event_type TEXT,
    driver TEXT,
    pause_duration TEXT,
    idling_duration TEXT,
    ignition_duration TEXT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_hash TEXT NOT NULL,
    PRIMARY KEY (id, event_time),
    UNIQUE (source_hash, event_time)
) PARTITION BY RANGE (event_time);

CREATE TABLE IF NOT EXISTS sync_runs (
    id BIGSERIAL PRIMARY KEY,
    started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    period_start TIMESTAMPTZ NOT NULL,
    period_end TIMESTAMPTZ NOT NULL,
    group_name TEXT,
    status TEXT NOT NULL,
    chunks_total INTEGER NOT NULL DEFAULT 0,
    chunks_success INTEGER NOT NULL DEFAULT 0,
    rows_received BIGINT NOT NULL DEFAULT 0,
    rows_inserted BIGINT NOT NULL DEFAULT 0,
    error_message TEXT
);

CREATE TABLE IF NOT EXISTS sync_chunks (
    id BIGSERIAL PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES sync_runs(id) ON DELETE CASCADE,
    chunk_start TIMESTAMPTZ NOT NULL,
    chunk_end TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL,
    rows_received INTEGER NOT NULL DEFAULT 0,
    rows_inserted INTEGER NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    duration_seconds DOUBLE PRECISION,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS ix_sync_chunks_run ON sync_chunks(run_id);

CREATE TABLE IF NOT EXISTS recalculation_queue (
    normalized_plate TEXT NOT NULL,
    day DATE NOT NULL,
    reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (normalized_plate, day)
);

CREATE TABLE IF NOT EXISTS gate_crossings (
    id BIGSERIAL PRIMARY KEY,
    normalized_plate TEXT NOT NULL,
    event_time TIMESTAMPTZ NOT NULL,
    gate_code TEXT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('ENTRY', 'EXIT')),
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    position geography(Point, 4326),
    confidence DOUBLE PRECISION,
    source TEXT NOT NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    algorithm_version TEXT NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (normalized_plate, event_time, gate_code, direction, algorithm_version)
);
CREATE INDEX IF NOT EXISTS ix_gate_crossings_time_plate ON gate_crossings(event_time, normalized_plate);

CREATE TABLE IF NOT EXISTS violations (
    id BIGSERIAL PRIMARY KEY,
    normalized_plate TEXT NOT NULL,
    violation_type TEXT NOT NULL,
    start_time TIMESTAMPTZ NOT NULL,
    finish_time TIMESTAMPTZ,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    position geography(Point, 4326),
    confidence DOUBLE PRECISION,
    severity INTEGER,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    algorithm_version TEXT NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_violations_time_plate ON violations(start_time, normalized_plate);
CREATE INDEX IF NOT EXISTS ix_violations_type_time ON violations(violation_type, start_time);

CREATE TABLE IF NOT EXISTS vehicle_daily_metrics (
    day DATE NOT NULL,
    normalized_plate TEXT NOT NULL,
    distance_km DOUBLE PRECISION NOT NULL DEFAULT 0,
    movement_seconds INTEGER NOT NULL DEFAULT 0,
    ignition_seconds INTEGER NOT NULL DEFAULT 0,
    idling_seconds INTEGER NOT NULL DEFAULT 0,
    parking_seconds INTEGER NOT NULL DEFAULT 0,
    first_activity TIMESTAMPTZ,
    last_activity TIMESTAMPTZ,
    first_entry_time TIMESTAMPTZ,
    last_exit_time TIMESTAMPTZ,
    entry_count INTEGER NOT NULL DEFAULT 0,
    exit_count INTEGER NOT NULL DEFAULT 0,
    violation_count INTEGER NOT NULL DEFAULT 0,
    gps_point_count INTEGER NOT NULL DEFAULT 0,
    data_quality_percent DOUBLE PRECISION,
    details JSONB NOT NULL DEFAULT '{}'::jsonb,
    algorithm_version TEXT NOT NULL,
    calculated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (day, normalized_plate)
);

CREATE TABLE IF NOT EXISTS algorithm_versions (
    algorithm_code TEXT NOT NULL,
    version TEXT NOT NULL,
    description TEXT,
    parameters JSONB NOT NULL DEFAULT '{}'::jsonb,
    activated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (algorithm_code, version)
);
