from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

from gp_p1_mig.workflow import cmd_init, cmd_ingest, cmd_reconcile


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    sandbox = root / "tmp" / "smoke"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)

    takeout_dir = sandbox / "Takeout" / "Google Photos" / "Album"
    takeout_dir.mkdir(parents=True)
    media = takeout_dir / "IMG_0001.jpg"
    # Minimal valid JPEG (SOI + APP0 JFIF + EOI)
    media.write_bytes(
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
    )
    sidecar = takeout_dir / "IMG_0001.jpg.json"
    sidecar.write_text(json.dumps({"photoTakenTime": {"timestamp": "1700000000"}, "geoDataExif": {"latitude": 25.0, "longitude": 121.5}}), encoding="utf-8")

    zip_path = sandbox / "sample_takeout.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.write(media, media.relative_to(sandbox))
        zf.write(sidecar, sidecar.relative_to(sandbox))

    db = sandbox / "data" / "state" / "state.db"
    cmd_init(sandbox, db)
    print(cmd_ingest(sandbox, db, zip_path))
    print(cmd_reconcile(db))


if __name__ == "__main__":
    main()
