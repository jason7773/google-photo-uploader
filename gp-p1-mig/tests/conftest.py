"""Shared fixtures for gp-p1-mig tests."""
from __future__ import annotations

import json
import struct
import zipfile
from pathlib import Path

import pytest

from gp_p1_mig.db import init_db


@pytest.fixture()
def workspace(tmp_path: Path):
    """Create a minimal workspace with initialized DB."""
    root = tmp_path / "repo"
    db_path = root / "data" / "state" / "state.db"
    for d in [
        root / "data" / "state",
        root / "data" / "work" / "extracted",
        root / "data" / "work" / "patched",
        root / "data" / "work" / "batches",
        root / "data" / "exports",
    ]:
        d.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    return root, db_path


def _minimal_jpeg(extra: bytes = b"") -> bytes:
    """Create a minimal valid JPEG that exiftool can process."""
    # SOI + APP0 (JFIF) + minimal content + EOI
    soi = b"\xff\xd8"
    app0_marker = b"\xff\xe0"
    app0_payload = b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    app0_len = struct.pack(">H", len(app0_payload) + 2)
    eoi = b"\xff\xd9"
    return soi + app0_marker + app0_len + app0_payload + extra + eoi


@pytest.fixture()
def takeout_zip(tmp_path: Path) -> Path:
    """Create a fake takeout zip with media + sidecar JSON."""
    media_dir = tmp_path / "takeout_content"
    media_dir.mkdir()
    img = media_dir / "IMG_0001.jpg"
    img.write_bytes(_minimal_jpeg(b"\x00" * 50))

    sidecar = media_dir / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1700000000"},
        "geoDataExif": {"latitude": 25.033, "longitude": 121.565},
    }), encoding="utf-8")

    img2 = media_dir / "IMG_0002.jpg"
    img2.write_bytes(_minimal_jpeg(b"\xff" * 50))

    sidecar2 = media_dir / "IMG_0002.jpg.json"
    sidecar2.write_text(json.dumps({
        "photoTakenTime": {"timestamp": "1700000100"},
        "geoDataExif": {"latitude": 0.0, "longitude": 0.0},
    }), encoding="utf-8")

    zip_path = tmp_path / "takeout.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in media_dir.rglob("*"):
            zf.write(f, f.relative_to(media_dir))

    return zip_path
