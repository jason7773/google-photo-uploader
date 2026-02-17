from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import random
import re
import shutil
import sqlite3
import subprocess
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .db import connect, init_db, transaction
from .tools import find_exiftool, find_ffmpeg, verify_tools

log = logging.getLogger(__name__)

MEDIA_EXTS = {".jpg", ".jpeg", ".heic", ".png", ".mp4", ".mov"}

# Optional progress callback: (current, total, description) -> None
ProgressCallback = Callable[[int, int, str], None] | None


class MigError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_workspace(root: Path) -> None:
    for p in [
        root / "data" / "state",
        root / "data" / "work" / "extracted",
        root / "data" / "work" / "patched",
        root / "data" / "work" / "batches",
        root / "data" / "exports",
    ]:
        p.mkdir(parents=True, exist_ok=True)


def default_db(root: Path) -> Path:
    return root / "data" / "state" / "state.db"


def cmd_init(root: Path, db_path: Path) -> str:
    ensure_workspace(root)
    init_db(db_path)
    log.info("工作空間已初始化: root=%s, db=%s", root, db_path)
    return f"initialized db: {db_path}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _find_sidecar(media_path: Path) -> Path | None:
    # Try standard .jpg.json format first
    p = media_path.with_name(media_path.name + ".json")
    if p.exists():
        return p
    # Try Google's newer .supplemental-metadata.json format
    p2 = media_path.with_suffix(media_path.suffix + ".supplemental-metadata.json")
    if p2.exists():
        return p2
    return None


def cmd_ingest(
    root: Path, db_path: Path, zip_path: Path,
    run_id: str | None = None, progress: ProgressCallback = None,
) -> dict:
    ensure_workspace(root)
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")
    zip_name = zip_path.stem
    extract_root = root / "data" / "work" / "extracted" / run_id / zip_name
    extract_root.mkdir(parents=True, exist_ok=True)
    log.info("開始匯入: zip=%s, run_id=%s", zip_path.name, run_id)

    with zipfile.ZipFile(zip_path, "r") as zf:
        # ZIP path traversal protection
        extract_root_str = str(extract_root.resolve())
        for member in zf.namelist():
            resolved = (extract_root / member).resolve()
            if not str(resolved).startswith(extract_root_str):
                raise MigError(f"Zip path traversal detected: {member}")
        zf.extractall(extract_root)
    log.info("ZIP 解壓完成: %s", extract_root)

    # Collect media files first for progress tracking
    media_files = [
        p for p in extract_root.rglob("*")
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS
    ]
    total = len(media_files)
    log.info("偵測到 %d 個媒體檔案", total)

    ingested = 0
    duplicates = 0
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(run_id, source_zip, extracted_root, status) VALUES (?, ?, ?, 'DONE')",
            (run_id, str(zip_path), str(extract_root)),
        )
        for i, path in enumerate(media_files, 1):
            if progress:
                progress(i, total, path.name)
            content_id = _sha256(path)
            sidecar = _find_sidecar(path)
            row = conn.execute("SELECT content_id FROM media_items WHERE content_id=?", (content_id,)).fetchone()
            if row:
                duplicates += 1
                conn.execute(
                    "INSERT INTO duplicates(content_id, dup_path, source_zip, ingest_run_id) VALUES (?, ?, ?, ?)",
                    (content_id, str(path), str(zip_path), run_id),
                )
                continue

            conn.execute(
                """
                INSERT INTO media_items(
                    content_id, original_name, ext, source_zip, extracted_path,
                    sidecar_path, has_sidecar, ingest_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    content_id,
                    path.name,
                    path.suffix.lower().lstrip("."),
                    str(zip_path),
                    str(path),
                    str(sidecar) if sidecar else None,
                    1 if sidecar else 0,
                    run_id,
                ),
            )
            ingested += 1

    log.info("匯入完成: scanned=%d, ingested=%d, duplicates=%d", total, ingested, duplicates)
    return {"run_id": run_id, "extract_root": str(extract_root), "scanned": total, "ingested": ingested, "duplicates": duplicates}


def _normalize_stem(name: str) -> str:
    stem = Path(name).stem
    # Strip Google's newer sidecar suffix
    stem = re.sub(r"\.supplemental-metadata$", "", stem, flags=re.I)
    stem = re.sub(r"\.(jpg|jpeg|heic|png|mp4|mov)$", "", stem, flags=re.I)
    stem = re.sub(r"\(\d+\)$", "", stem)
    stem = re.sub(r"[-_ ]+", "", stem)
    return stem.lower()



def _parse_sidecar(sidecar: Path) -> tuple[int | None, float | None, float | None]:
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    ts = None
    lat = None
    lng = None

    for key in ["photoTakenTime", "creationTime"]:
        if key in data and isinstance(data[key], dict):
            raw = data[key].get("timestamp")
            if raw is not None:
                try:
                    ts = int(raw)
                    break
                except ValueError:
                    pass
    geo = data.get("geoDataExif") or data.get("geoData") or {}
    lat = geo.get("latitude")
    lng = geo.get("longitude")
    # Google Takeout uses (0, 0) to represent "no location"
    if lat == 0.0 and lng == 0.0:
        lat, lng = None, None
    return ts, lat, lng


def cmd_reconcile(db_path: Path) -> dict:
    matched = 0
    parsed = 0
    log.info("開始比對 sidecar")
    with transaction(db_path) as conn:
        rows = conn.execute("SELECT id, extracted_path, sidecar_path, has_sidecar FROM media_items").fetchall()
        log.info("共 %d 筆待比對", len(rows))
        by_dir: dict[str, list[Path]] = {}
        for r in rows:
            path = Path(r["extracted_path"])
            by_dir.setdefault(str(path.parent), [])
        for d in list(by_dir):
            by_dir[d] = list(Path(d).glob("*.json"))

        for r in rows:
            media_path = Path(r["extracted_path"])
            if not r["has_sidecar"]:
                norm = _normalize_stem(media_path.name)
                candidate = None
                for json_path in by_dir.get(str(media_path.parent), []):
                    if _normalize_stem(json_path.name) == norm:
                        candidate = json_path
                        break
                if candidate:
                    conn.execute(
                        "UPDATE media_items SET sidecar_path=?, has_sidecar=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (str(candidate), r["id"]),
                    )
                    matched += 1
                    sidecar = candidate
                else:
                    continue
            else:
                sidecar = Path(r["sidecar_path"])

            if sidecar.exists():
                ts, lat, lng = _parse_sidecar(sidecar)
                conn.execute(
                    """
                    UPDATE media_items
                    SET expected_taken_epoch=?, expected_lat=?, expected_lng=?,
                        patch_status=CASE WHEN patch_status='NEW' THEN 'READY' ELSE patch_status END,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                    """,
                    (ts, lat, lng, r["id"]),
                )
                parsed += 1
    log.info("比對完成: matched=%d, parsed=%d", matched, parsed)
    return {"matched_sidecar": matched, "parsed": parsed}


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def _verify_patch(
    exiftool_bin: str, dst: Path,
    expected_epoch: int | None,
    expected_lat: float | None, expected_lng: float | None,
) -> str | None:
    """Read back metadata from *dst* and compare against expectations.

    Returns ``None`` on success or a human-readable diff string on mismatch.
    """
    rc, stdout, _ = _run([
        exiftool_bin, "-j", "-n",   # -n = numeric output (no formatting)
        "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude",
        str(dst),
    ])
    if rc != 0:
        return "exiftool read-back failed"
    try:
        data = json.loads(stdout)
        if not data:
            return "exiftool returned empty JSON"
        info = data[0]
    except (json.JSONDecodeError, IndexError):
        return "exiftool JSON parse error"

    issues: list[str] = []

    # --- Verify timestamp (allow ±2 seconds) ---
    if expected_epoch:
        dto_str = info.get("DateTimeOriginal", "")
        if dto_str:
            try:
                # exiftool returns "YYYY:MM:DD HH:MM:SS"
                dt = datetime.strptime(str(dto_str), "%Y:%m:%d %H:%M:%S")
                actual_epoch = int(dt.replace(tzinfo=timezone.utc).timestamp())
                if abs(actual_epoch - expected_epoch) > 2:
                    issues.append(f"timestamp mismatch: expected={expected_epoch}, actual={actual_epoch}")
            except ValueError:
                issues.append(f"cannot parse DateTimeOriginal: {dto_str}")
        else:
            issues.append("DateTimeOriginal not found after patch")

    # --- Verify GPS (allow ±0.001°) ---
    if expected_lat is not None and expected_lng is not None:
        actual_lat = info.get("GPSLatitude")
        actual_lng = info.get("GPSLongitude")
        if actual_lat is None or actual_lng is None:
            issues.append("GPS not found after patch")
        else:
            try:
                if abs(float(actual_lat) - expected_lat) > 0.001:
                    issues.append(f"latitude mismatch: expected={expected_lat}, actual={actual_lat}")
                if abs(float(actual_lng) - expected_lng) > 0.001:
                    issues.append(f"longitude mismatch: expected={expected_lng}, actual={actual_lng}")
            except (ValueError, TypeError):
                issues.append(f"GPS parse error: lat={actual_lat}, lng={actual_lng}")

    return "; ".join(issues) if issues else None


def cmd_patch(
    root: Path, db_path: Path,
    exiftool_bin: str | None = None, ffmpeg_bin: str | None = None,
    progress: ProgressCallback = None,
) -> dict:
    # Auto-detect tool paths if not explicitly provided
    exiftool_bin = exiftool_bin or find_exiftool()
    ffmpeg_bin = ffmpeg_bin or find_ffmpeg()
    log.info("exiftool: %s", exiftool_bin)
    log.info("ffmpeg: %s", ffmpeg_bin)

    patched_dir = root / "data" / "work" / "patched"
    patched_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    fail = 0
    verified = 0
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT id, content_id, ext, extracted_path, expected_taken_epoch, expected_lat, expected_lng FROM media_items WHERE patch_status='READY'"
        ).fetchall()
        total = len(rows)
        log.info("共 %d 筆待 patch", total)
        for idx, r in enumerate(rows, 1):
            if progress:
                progress(idx, total, Path(r["extracted_path"]).name)
            src = Path(r["extracted_path"])
            dst = patched_dir / f"{r['content_id']}.{r['ext']}"
            shutil.copy2(src, dst)
            err = ""
            is_image = r["ext"] in {"jpg", "jpeg", "heic", "png"}

            if is_image:
                # ── Image patch via exiftool ──
                cmd = [exiftool_bin, "-overwrite_original", "-m"]
                if r["expected_taken_epoch"]:
                    ts = datetime.fromtimestamp(r["expected_taken_epoch"], tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
                    cmd += [f"-DateTimeOriginal={ts}"]
                if r["expected_lat"] is not None and r["expected_lng"] is not None:
                    lat, lng = r["expected_lat"], r["expected_lng"]
                    cmd += [
                        f"-GPSLatitude={abs(lat)}",
                        f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                        f"-GPSLongitude={abs(lng)}",
                        f"-GPSLongitudeRef={'E' if lng >= 0 else 'W'}",
                    ]
                cmd += [str(dst)]
                rc, _, stderr = _run(cmd)
                if rc != 0:
                    err = stderr
            else:
                # ── Video patch via ffmpeg ──
                tmp = dst.with_suffix(".tmp" + dst.suffix)
                meta: list[tuple[str, str]] = []
                if r["expected_taken_epoch"]:
                    meta.append(("creation_time", datetime.fromtimestamp(r["expected_taken_epoch"], tz=timezone.utc).isoformat()))
                if r["expected_lat"] is not None and r["expected_lng"] is not None:
                    lat, lng = r["expected_lat"], r["expected_lng"]
                    sign_lat = "+" if lat >= 0 else ""
                    sign_lng = "+" if lng >= 0 else ""
                    location_str = f"{sign_lat}{lat}{sign_lng}{lng}/"
                    meta.append(("location", location_str))
                    meta.append(("location-eng", location_str))
                cmd = [ffmpeg_bin, "-y", "-i", str(dst)]
                for k, v in meta:
                    cmd += ["-metadata", f"{k}={v}"]
                cmd += ["-codec", "copy", str(tmp)]
                rc, _, stderr = _run(cmd)
                if rc == 0:
                    os.replace(tmp, dst)
                else:
                    err = stderr
                    if tmp.exists():
                        tmp.unlink()

            # ── Post-patch verification (images only; video metadata is harder to read back) ──
            if not err and is_image:
                verify_err = _verify_patch(
                    exiftool_bin, dst,
                    r["expected_taken_epoch"], r["expected_lat"], r["expected_lng"],
                )
                if verify_err:
                    err = f"verify-failed: {verify_err}"
                else:
                    verified += 1

            if err:
                conn.execute(
                    "UPDATE media_items SET patch_status='FAILED', patch_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (err[:2000], r["id"]),
                )
                fail += 1
            else:
                conn.execute(
                    "UPDATE media_items SET patch_status='PATCHED', patched_path=?, patch_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(dst), r["id"]),
                )
                ok += 1
    log.info("Patch 完成: ok=%d, failed=%d, verified=%d", ok, fail, verified)
    return {"patched": ok, "failed": fail, "verified": verified}


def _next_batch_id(conn: sqlite3.Connection) -> str:
    row = conn.execute("SELECT batch_id FROM batches ORDER BY batch_id DESC LIMIT 1").fetchone()
    if row:
        return f"B{int(row['batch_id'][1:]) + 1:04d}"
    return "B0001"


def cmd_make_batch(root: Path, db_path: Path, max_bytes: int, max_files: int) -> dict:
    batches_root = root / "data" / "work" / "batches"
    batches_root.mkdir(parents=True, exist_ok=True)
    selected: list[tuple[str, Path, int]] = []
    total_bytes = 0
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT content_id, patched_path FROM media_items WHERE patch_status='PATCHED' AND batch_id IS NULL ORDER BY created_at"
        ).fetchall()
        for r in rows:
            p = Path(r["patched_path"])
            if not p.exists():
                continue
            size = p.stat().st_size
            if len(selected) >= max_files or total_bytes + size > max_bytes:
                break
            selected.append((r["content_id"], p, size))
            total_bytes += size
        if not selected:
            raise MigError("no PATCHED items available for batching")

        batch_id = _next_batch_id(conn)
        batch_root = batches_root / batch_id
        files_dir = batch_root / "files"
        files_dir.mkdir(parents=True, exist_ok=True)

        conn.execute(
            "INSERT INTO batches(batch_id, status, max_files, max_bytes, total_files, total_bytes, local_batch_path) VALUES (?, 'CREATED', ?, ?, ?, ?, ?)",
            (batch_id, max_files, max_bytes, len(selected), total_bytes, str(batch_root)),
        )

        manifest_csv = batch_root / "batch_manifest.csv"
        plan_json = batch_root / "push_plan.json"
        with manifest_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["batch_id", "content_id", "file_name", "file_size", "source_patched_path"])
            for content_id, src, size in selected:
                dst = files_dir / src.name
                shutil.copy2(src, dst)
                w.writerow([batch_id, content_id, dst.name, size, str(src)])
                conn.execute(
                    "INSERT INTO batch_items(batch_id, content_id, file_name, file_size) VALUES (?, ?, ?, ?)",
                    (batch_id, content_id, dst.name, size),
                )
                conn.execute("UPDATE media_items SET batch_id=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?", (batch_id, content_id))

        plan_json.write_text(
            json.dumps(
                {
                    "batch_id": batch_id,
                    "generated_at": now_iso(),
                    "total_files": len(selected),
                    "total_bytes": total_bytes,
                    "local_files_dir": str(files_dir),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    log.info("批次 %s 建立完成: %d 檔, %d bytes", batch_id, len(selected), total_bytes)
    return {"batch_id": batch_id, "total_files": len(selected), "total_bytes": total_bytes}


def cmd_push(root: Path, db_path: Path, batch_id: str, device_path: str, adb_bin: str = "adb") -> dict:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise MigError(f"batch not found: {batch_id}")
        files_dir = Path(row["local_batch_path"]) / "files"
        if not files_dir.exists():
            raise MigError(f"batch files missing: {files_dir}")
        rc, _, stderr = _run([adb_bin, "push", str(files_dir), device_path])
        if rc != 0:
            raise MigError(f"adb push failed: {stderr}")
        conn.execute(
            "UPDATE batches SET pushed_at=?, status='PUSHED', device_target_path=? WHERE batch_id=?",
            (now_iso(), device_path, batch_id),
        )
    log.info("批次 %s 推送完成 -> %s", batch_id, device_path)
    return {"batch_id": batch_id, "device_path": device_path}


def cmd_export_verify(root: Path, db_path: Path, batch_id: str, sample_size: int = 30) -> dict:
    exports = root / "data" / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    with transaction(db_path) as conn:
        items = conn.execute(
            "SELECT bi.content_id, bi.file_name, bi.file_size, m.expected_taken_epoch FROM batch_items bi JOIN media_items m ON m.content_id=bi.content_id WHERE bi.batch_id=?",
            (batch_id,),
        ).fetchall()
        if not items:
            raise MigError(f"no items in batch: {batch_id}")

        checklist = exports / f"{batch_id}_checklist.csv"
        samples = exports / f"{batch_id}_sample_items.csv"

        with checklist.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["batch_id", "content_id", "file_name", "check_uploaded", "check_taken_time", "check_gps", "note"])
            for it in items:
                w.writerow([batch_id, it["content_id"], it["file_name"], "", "", "", ""])

        pick = random.sample(list(items), k=min(sample_size, len(items)))
        with samples.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["batch_id", "content_id", "file_name", "file_size", "expected_taken_epoch", "result", "note"])
            for it in pick:
                w.writerow([batch_id, it["content_id"], it["file_name"], it["file_size"], it["expected_taken_epoch"], "", ""])
    return {"checklist": str(checklist), "samples": str(samples), "sample_count": min(sample_size, len(items))}


def cmd_import_verify(db_path: Path, batch_id: str, result_csv: Path) -> dict:
    passed = True
    with result_csv.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        if not rows:
            raise MigError("result csv is empty")
        for r in rows:
            if r.get("result", "").strip().lower() not in {"ok", "pass", "passed", ""}:
                passed = False

    with transaction(db_path) as conn:
        if passed:
            conn.execute("UPDATE batches SET status='VERIFIED', verified_at=? WHERE batch_id=?", (now_iso(), batch_id))
            status = "VERIFIED"
        else:
            status = "PUSHED"
    return {"batch_id": batch_id, "status": status, "rows": len(rows)}


def cmd_purge(root: Path, db_path: Path, batch_id: str, purge_patched: bool = False) -> dict:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT status, local_batch_path FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise MigError(f"batch not found: {batch_id}")
        if row["status"] != "VERIFIED":
            raise MigError("purge is only allowed for VERIFIED batch")

        files_dir = Path(row["local_batch_path"]) / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir)

        removed_patched = 0
        if purge_patched:
            items = conn.execute("SELECT patched_path FROM media_items WHERE batch_id=?", (batch_id,)).fetchall()
            for it in items:
                if it["patched_path"]:
                    p = Path(it["patched_path"])
                    if p.exists():
                        p.unlink()
                        removed_patched += 1

        conn.execute("UPDATE batches SET status='PURGED', purged_at=? WHERE batch_id=?", (now_iso(), batch_id))

    log.info("清除完成: batch=%s, removed_patched=%d", batch_id, removed_patched)
    return {"batch_id": batch_id, "purged_files_dir": str(files_dir), "removed_patched": removed_patched}


def cmd_retry_failed(
    root: Path, db_path: Path,
    exiftool_bin: str | None = None, ffmpeg_bin: str | None = None,
    progress: ProgressCallback = None,
) -> dict:
    """Reset FAILED items to READY and re-run patch."""
    with transaction(db_path) as conn:
        n = conn.execute(
            "UPDATE media_items SET patch_status='READY', patch_error=NULL, updated_at=CURRENT_TIMESTAMP WHERE patch_status='FAILED'"
        ).rowcount
    log.info("已重置 %d 筆 FAILED -> READY", n)
    if n == 0:
        return {"reset": 0, "patched": 0, "failed": 0, "verified": 0}
    result = cmd_patch(root, db_path, exiftool_bin, ffmpeg_bin, progress=progress)
    result["reset"] = n
    return result
