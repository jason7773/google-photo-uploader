"""Tests for workflow.py — ingest, reconcile, sidecar parsing, (0,0) filter, retry-failed."""
from __future__ import annotations

import json
import sqlite3
import zipfile
from pathlib import Path

import pytest

from gp_p1_mig.db import connect, init_db, transaction
from gp_p1_mig.workflow import (
    MigError,
    _match_by_title,
    _parse_sidecar,
    cmd_ingest,
    cmd_init,
    cmd_reconcile,
    cmd_retry_failed,
)


# ── Init ──

def test_cmd_init(tmp_path: Path):
    root = tmp_path / "repo"
    db_path = root / "data" / "state" / "state.db"
    result = cmd_init(root, db_path)
    assert "initialized" in result
    assert db_path.exists()


# ── Sidecar parsing ──

def test_parse_sidecar_normal(tmp_path: Path):
    sc = tmp_path / "test.json"
    sc.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1700000000"},
        "geoDataExif": {"latitude": 25.033, "longitude": 121.565},
    }), encoding="utf-8")
    ts, lat, lng = _parse_sidecar(sc)
    assert ts == 1700000000
    assert lat == pytest.approx(25.033)
    assert lng == pytest.approx(121.565)


def test_parse_sidecar_zero_gps(tmp_path: Path):
    sc = tmp_path / "test.json"
    sc.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1700000000"},
        "geoDataExif": {"latitude": 0.0, "longitude": 0.0},
    }), encoding="utf-8")
    ts, lat, lng = _parse_sidecar(sc)
    assert ts == 1700000000
    assert lat is None
    assert lng is None


def test_parse_sidecar_no_geo(tmp_path: Path):
    sc = tmp_path / "test.json"
    sc.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1700000000"},
    }), encoding="utf-8")
    ts, lat, lng = _parse_sidecar(sc)
    assert ts == 1700000000
    assert lat is None
    assert lng is None


# ── Ingest ──

def test_cmd_ingest(workspace, takeout_zip):
    root, db_path = workspace
    result = cmd_ingest(root, db_path, takeout_zip)
    assert result["scanned"] == 2
    assert result["ingested"] == 2
    assert result["duplicates"] == 0


def test_cmd_ingest_duplicates(workspace, takeout_zip):
    root, db_path = workspace
    cmd_ingest(root, db_path, takeout_zip, run_id="r1")
    result = cmd_ingest(root, db_path, takeout_zip, run_id="r2")
    assert result["duplicates"] == 2
    assert result["ingested"] == 0


def test_cmd_ingest_progress_callback(workspace, takeout_zip):
    root, db_path = workspace
    calls = []
    result = cmd_ingest(root, db_path, takeout_zip, progress=lambda c, t, d: calls.append((c, t)))
    assert len(calls) == result["scanned"]
    assert calls[-1][0] == calls[-1][1]  # last call: current == total


# ── Reconcile ──

def test_cmd_reconcile(workspace, takeout_zip):
    root, db_path = workspace
    cmd_ingest(root, db_path, takeout_zip)
    result = cmd_reconcile(db_path)
    assert result["parsed"] >= 1


def test_reconcile_zero_gps_filtered(workspace, takeout_zip):
    """(0,0) GPS should be stored as NULL after reconcile."""
    root, db_path = workspace
    cmd_ingest(root, db_path, takeout_zip)
    cmd_reconcile(db_path)
    conn = connect(db_path)
    rows = conn.execute("SELECT original_name, expected_lat, expected_lng FROM media_items ORDER BY original_name").fetchall()
    conn.close()
    # IMG_0001 has real GPS
    assert rows[0]["expected_lat"] == pytest.approx(25.033)
    # IMG_0002 had (0,0) → should be None
    assert rows[1]["expected_lat"] is None
    assert rows[1]["expected_lng"] is None


def test_reconcile_truncated_filename(workspace, takeout_zip_truncated):
    """Truncated media + sidecar filenames should still match via JSON title."""
    root, db_path = workspace
    cmd_ingest(root, db_path, takeout_zip_truncated)
    result = cmd_reconcile(db_path)
    # Should match via title-based fallback and parse the sidecar
    assert result["matched_sidecar"] >= 1
    assert result["parsed"] >= 1
    # Verify the item moved from NEW to READY
    from gp_p1_mig.db import connect
    conn = connect(db_path)
    row = conn.execute(
        "SELECT patch_status, has_sidecar FROM media_items WHERE original_name LIKE '%instagra%'"
    ).fetchone()
    conn.close()
    assert row["patch_status"] == "READY"
    assert row["has_sidecar"] == 1


# ── ZIP path traversal ──

def test_zip_traversal_blocked(workspace, tmp_path: Path):
    root, db_path = workspace
    evil_zip = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil_zip, "w") as zf:
        zf.writestr("../../../etc/passwd", "hacked")
    with pytest.raises(MigError, match="traversal"):
        cmd_ingest(root, db_path, evil_zip)


# ── Retry-failed ──

def test_retry_failed_no_failures(workspace):
    root, db_path = workspace
    result = cmd_retry_failed(root, db_path)
    assert result["reset"] == 0
