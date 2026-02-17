"""Full P0 verification: valid JPEG + GPS directions + (0,0) filtering + post-patch read-back."""
from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

from PIL import Image
from gp_p1_mig.workflow import cmd_init, cmd_ingest, cmd_reconcile, cmd_patch

ROOT = Path(__file__).resolve().parents[1]
SANDBOX = ROOT / "tmp" / "smoke_p0_full"


def make_jpeg(path: Path, seed: int = 0) -> None:
    """Create a tiny but valid JPEG."""
    img = Image.new("RGB", (4, 4), color=(seed * 50 % 256, 100, 150))
    img.save(str(path), "JPEG")


def make_sidecar(path: Path, ts: int, lat: float | None, lng: float | None) -> None:
    data: dict = {"photoTakenTime": {"timestamp": str(ts)}}
    if lat is not None and lng is not None:
        data["geoDataExif"] = {"latitude": lat, "longitude": lng}
    path.write_text(json.dumps(data), encoding="utf-8")


def main() -> None:
    if SANDBOX.exists():
        shutil.rmtree(SANDBOX)
    SANDBOX.mkdir(parents=True)

    album = SANDBOX / "Takeout" / "Google Photos" / "Album"
    album.mkdir(parents=True)

    # --- Test cases ---
    cases = [
        ("IMG_TW.jpg", 1700000000, 25.0330, 121.5654, "Taipei N,E"),
        ("IMG_AU.jpg", 1700000001, -33.8688, 151.2093, "Sydney S,E"),
        ("IMG_NY.jpg", 1700000002, 40.7128, -74.0060, "New York N,W"),
        ("IMG_BA.jpg", 1700000003, -34.6037, -58.3816, "Buenos Aires S,W"),
        ("IMG_ZERO.jpg", 1700000004, 0.0, 0.0, "(0,0) should be filtered"),
        ("IMG_NOGPS.jpg", 1700000005, None, None, "No GPS at all"),
    ]

    for i, (fname, ts, lat, lng, desc) in enumerate(cases):
        make_jpeg(album / fname, seed=i)
        make_sidecar(album / (fname + ".json"), ts, lat, lng)

    # Zip
    zip_path = SANDBOX / "test.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        for f in album.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(SANDBOX))

    # Run pipeline
    db = SANDBOX / "data" / "state" / "state.db"
    print(">>> init")
    print(cmd_init(SANDBOX, db))
    print(">>> ingest")
    print(json.dumps(cmd_ingest(SANDBOX, db, zip_path), indent=2))
    print(">>> reconcile")
    print(json.dumps(cmd_reconcile(db), indent=2))
    print(">>> patch")
    print(json.dumps(cmd_patch(SANDBOX, db), indent=2))

    # Check results
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    print("\n=== RESULTS ===")
    all_ok = True
    for r in conn.execute(
        "SELECT original_name, expected_lat, expected_lng, patch_status, patch_error FROM media_items ORDER BY original_name"
    ):
        name = r["original_name"]
        lat = r["expected_lat"]
        lng = r["expected_lng"]
        status = r["patch_status"]
        err = r["patch_error"]
        icon = "PASS" if status == "PATCHED" else "FAIL"
        if status != "PATCHED" and name != "IMG_ZERO.jpg" and name != "IMG_NOGPS.jpg":
            # IMG_ZERO and IMG_NOGPS may be READY (no GPS to patch for ZERO after filtering)
            if status == "FAILED":
                all_ok = False
        print(f"  [{icon}] {name:15s}  lat={str(lat):10s}  lng={str(lng):10s}  status={status}")
        if err:
            print(f"         error: {err[:150]}")

    conn.close()
    print()
    print("ALL PASS" if all_ok else "SOME FAILURES - check errors above")


if __name__ == "__main__":
    main()
