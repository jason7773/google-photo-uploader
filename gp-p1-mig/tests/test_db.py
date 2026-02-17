"""Tests for db.py — schema init and transaction context manager."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from gp_p1_mig.db import connect, init_db, transaction


def test_init_db_creates_tables(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    conn = connect(db)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    conn.close()
    assert "media_items" in tables
    assert "batches" in tables
    assert "batch_items" in tables
    assert "duplicates" in tables
    assert "ingest_runs" in tables


def test_init_db_idempotent(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    init_db(db)  # should not raise


def test_transaction_commits(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    with transaction(db) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(run_id, source_zip, extracted_root, status) VALUES ('r1', 'z.zip', '/tmp', 'DONE')"
        )
    conn2 = connect(db)
    assert conn2.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 1
    conn2.close()


def test_transaction_rollback(tmp_path: Path):
    db = tmp_path / "test.db"
    init_db(db)
    with pytest.raises(ValueError):
        with transaction(db) as conn:
            conn.execute(
                "INSERT INTO ingest_runs(run_id, source_zip, extracted_root, status) VALUES ('r1', 'z.zip', '/tmp', 'DONE')"
            )
            raise ValueError("deliberate")
    conn2 = connect(db)
    assert conn2.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 0
    conn2.close()
