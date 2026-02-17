PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    ext TEXT NOT NULL,
    source_zip TEXT,
    extracted_path TEXT NOT NULL,
    sidecar_path TEXT,
    has_sidecar INTEGER NOT NULL DEFAULT 0,
    expected_taken_epoch INTEGER,
    expected_lat REAL,
    expected_lng REAL,
    ingest_run_id TEXT,
    patch_status TEXT NOT NULL DEFAULT 'NEW',
    patch_error TEXT,
    patched_path TEXT,
    batch_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS duplicates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content_id TEXT NOT NULL,
    dup_path TEXT NOT NULL,
    source_zip TEXT,
    ingest_run_id TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(content_id) REFERENCES media_items(content_id)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'CREATED',
    max_files INTEGER,
    max_bytes INTEGER,
    total_files INTEGER NOT NULL DEFAULT 0,
    total_bytes INTEGER NOT NULL DEFAULT 0,
    local_batch_path TEXT NOT NULL,
    device_target_path TEXT,
    pushed_at TEXT,
    verified_at TEXT,
    purged_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS batch_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    content_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(batch_id) REFERENCES batches(batch_id),
    FOREIGN KEY(content_id) REFERENCES media_items(content_id)
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    run_id TEXT PRIMARY KEY,
    source_zip TEXT NOT NULL,
    extracted_root TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DONE',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_media_patch_status ON media_items(patch_status);
CREATE INDEX IF NOT EXISTS idx_media_batch_id ON media_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_batch_status ON batches(status);
