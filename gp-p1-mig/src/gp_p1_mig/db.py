from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

# SQL schema embedded as Python constant so it works after pip install / PyInstaller.
# This is the single source of truth; keep in sync with sql/init.sql if editing.
_SCHEMA = """\
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
-- Composite index: speeds up cmd_patch / cmd_make_batch queries on (patch_status, batch_id)
CREATE INDEX IF NOT EXISTS idx_media_status_batch ON media_items(patch_status, batch_id);

-- Schema versioning for future migrations
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
INSERT OR IGNORE INTO schema_version(version) VALUES (1);
"""

# Future migrations: add entries as (version, sql) tuples.
# Each migration should be idempotent (use IF NOT EXISTS, etc.)
_MIGRATIONS: list[tuple[int, str]] = [
    # version 2: add composite index + switch to WAL for existing databases
    (2, """
        CREATE INDEX IF NOT EXISTS idx_media_status_batch
            ON media_items(patch_status, batch_id);
        PRAGMA journal_mode=WAL;
    """),
]


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    # ── Performance tuning ──────────────────────────────────────────────────
    # WAL: readers don't block writer; writer doesn't block readers.
    # Essential when the web UI polls /api/state while a task is running.
    conn.execute("PRAGMA journal_mode=WAL")
    # NORMAL: safe on power-loss (WAL checkpoint survives), faster than FULL
    conn.execute("PRAGMA synchronous=NORMAL")
    # 64 MB page cache (negative value = KiB)
    conn.execute("PRAGMA cache_size=-65536")
    # Keep temp tables / sort buffers in RAM instead of a temp file
    conn.execute("PRAGMA temp_store=MEMORY")
    # 256 MB memory-mapped I/O — reduces syscall overhead on large DB scans
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


@contextmanager
def transaction(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Context manager that commits on success, rolls back on error, and always closes."""
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with transaction(db_path) as conn:
        conn.executescript(_SCHEMA)
    migrate_db(db_path)


def migrate_db(db_path: Path) -> None:
    """Apply any pending schema migrations in order."""
    if not _MIGRATIONS:
        return
    with transaction(db_path) as conn:
        try:
            row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            current = row[0] if row and row[0] else 1
        except Exception:
            current = 0
        for ver, sql in sorted(_MIGRATIONS):
            if ver > current:
                conn.executescript(sql)
                conn.execute(
                    "INSERT OR REPLACE INTO schema_version(version) VALUES (?)", (ver,)
                )

