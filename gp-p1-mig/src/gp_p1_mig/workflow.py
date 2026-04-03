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
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from .db import connect, init_db, transaction
from .exiftool_session import ExifToolSession
from .tools import find_exiftool, find_ffmpeg, verify_tools

log = logging.getLogger(__name__)

# Google Photos 支援的所有格式
IMAGE_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".gif", ".webp",
    ".bmp", ".ico", ".tiff", ".tif",
    # RAW 格式
    ".dng", ".cr2", ".cr3", ".nef", ".arw", ".crw",
    ".orf", ".raf", ".pef", ".srw", ".rw2",
}
VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".mpg", ".mpeg",
    ".mod", ".mmv", ".tod", ".wmv", ".asf",
    ".divx", ".m4v", ".3gp", ".3g2",
    ".m2t", ".m2ts", ".mts",
}
MEDIA_EXTS = IMAGE_EXTS | VIDEO_EXTS

# Optional progress callback: (current, total, description) -> None
ProgressCallback = Callable[[int, int, str], None] | None


class MigError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _detect_real_ext(path: Path) -> str:
    """Read file magic bytes to detect actual format regardless of extension."""
    try:
        with open(path, "rb") as f:
            header = f.read(12)
        if header[:3] == b'\xff\xd8\xff':
            return "jpg"
        if header[:8] == b'\x89PNG\r\n\x1a\n':
            return "png"
        if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
            return "webp"
        if header[4:8] == b'ftyp':
            brand = header[8:12]
            # HEIC / HEIF family (must check before MOV/MP4)
            if brand in (b'heic', b'heis', b'heim', b'hevc', b'mif1', b'msf1'):
                return "heic"
            # QuickTime MOV vs MPEG-4
            return "mov" if brand == b'qt  ' else "mp4"
        if header[4:8] in (b'moov', b'mdat'):
            # Older containers without ftyp — trust the file's own extension
            return path.suffix.lower().lstrip(".")
    except OSError:
        pass
    return path.suffix.lower().lstrip(".")


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


# Parallel workers for hash computation.
# 8 is optimal for SSD; reduce to 2–4 for HDD to avoid seek thrashing.
_INGEST_WORKERS = 8


def _hash_and_detect(path: Path) -> tuple[str, str]:
    """Compute SHA-256 hash and detect real extension in a single file open.

    Combines ``_sha256`` + ``_detect_real_ext`` into one pass to halve I/O.
    Returns ``(sha256_hex, ext_without_dot)``.
    Safe to call from multiple threads (no shared mutable state).
    """
    h = hashlib.sha256()
    fallback = path.suffix.lower().lstrip(".")
    with path.open("rb") as f:
        first = f.read(1024 * 1024)
        h.update(first)
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    magic = first[:12]
    if magic[:3] == b'\xff\xd8\xff':
        ext = "jpg"
    elif magic[:8] == b'\x89PNG\r\n\x1a\n':
        ext = "png"
    elif magic[:4] == b'RIFF' and magic[8:12] == b'WEBP':
        ext = "webp"
    elif magic[4:8] == b'ftyp':
        brand = magic[8:12]
        # HEIC / HEIF family (must check before MOV/MP4)
        if brand in (b'heic', b'heis', b'heim', b'hevc', b'mif1', b'msf1'):
            ext = "heic"
        elif brand == b'qt  ':
            ext = "mov"
        else:
            ext = "mp4"
    elif magic[4:8] in (b'moov', b'mdat'):
        # Older containers without ftyp box — trust fallback extension
        ext = fallback if fallback in ("mov", "mp4") else "mp4"
    else:
        ext = fallback
    return h.hexdigest(), ext


def _find_sidecar(media_path: Path) -> Path | None:
    # Try standard .jpg.json format first
    p = media_path.with_name(media_path.name + ".json")
    if p.exists():
        return p
    # Try Google's newer .supplemental-metadata.json format
    # IMPORTANT: Use with_name() not with_suffix() because we want to APPEND, not REPLACE
    # e.g. IMG_123.jpg -> IMG_123.jpg.supplemental-metadata.json (not IMG_123.supplemental-metadata.json)
    p2 = media_path.with_name(media_path.name + ".supplemental-metadata.json")
    if p2.exists():
        return p2
    return None


def _match_by_title(media_path: Path, json_path: Path) -> bool:
    """Check if a JSON sidecar's ``title`` field matches the media file.

    Google Takeout truncates long filenames at different positions for the
    media file and its sidecar JSON.  The JSON always contains a ``title``
    key with the *original* (un-truncated) filename, so we can compare the
    truncated media stem against the full title stem.
    """
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
        title = data.get("title", "")
        if not title:
            return False
        title_stem = Path(title).stem.lower()
        media_stem = media_path.stem.lower()
        # The media filename is a prefix of the original title
        # (both may be truncated, but the title is always longer or equal)
        return len(media_stem) >= 10 and title_stem.startswith(media_stem)
    except (json.JSONDecodeError, OSError):
        return False


def cmd_ingest(
    root: Path, db_path: Path, zip_path: Path | None = None,
    run_id: str | None = None, progress: ProgressCallback = None,
    zip_paths: list[Path] | None = None,
) -> dict:
    """Ingest one or more Takeout ZIP files.

    Accepts either a single ``zip_path`` (backward compatible) or a list of
    ``zip_paths``.  When both are provided, ``zip_paths`` takes precedence.
    """
    ensure_workspace(root)
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S")

    # Normalize to a list
    if zip_paths:
        zips = list(zip_paths)
    elif zip_path:
        zips = [zip_path]
    else:
        raise MigError("請指定至少一個 ZIP 檔案路徑")

    # Extract all ZIPs into the SAME directory so cross-ZIP folders merge
    # (e.g. "2013年的相片" from ZIP#1 and ZIP#2 will end up together)
    extract_root = root / "data" / "work" / "extracted"
    extract_root.mkdir(parents=True, exist_ok=True)
    log.info(
        "開始匯入 %d 個 ZIP: %s, run_id=%s, extract_to=%s",
        len(zips), ", ".join(z.name for z in zips), run_id, extract_root,
    )

    # --- Phase 1: Extract all ZIPs and collect media files ---
    # Each entry is (full_path, source_zip_path)
    media_files: list[tuple[Path, Path]] = []
    for zp in zips:
        log.info("解壓中: %s ...", zp.name)
        with zipfile.ZipFile(zp, "r") as zf:
            # ZIP path traversal protection
            extract_root_str = str(extract_root.resolve())
            for member in zf.namelist():
                resolved = (extract_root / member).resolve()
                if not str(resolved).startswith(extract_root_str):
                    raise MigError(f"Zip path traversal detected: {member}")
            zf.extractall(extract_root)

            # Collect media files ONLY from this ZIP (not the entire extracted dir)
            for member in zf.namelist():
                if member.endswith('/') or member.endswith('\\'):
                    continue
                full_path = (extract_root / member).resolve()
                if full_path.is_file() and full_path.suffix.lower() in MEDIA_EXTS:
                    media_files.append((full_path, zp))
        log.info("ZIP 解壓完成: %s", zp.name)

    total = len(media_files)
    log.info("偵測到 %d 個媒體檔案 (共 %d 個 ZIP)", total, len(zips))

    # ── Phase 2a: Parallel hash + extension detection (I/O bound) ────────────
    # ThreadPoolExecutor: each worker reads one file for SHA-256 + magic byte
    # detection. DB writes remain sequential on the main thread.
    log.info("正在並行計算 %d 個檔案的雜湊値 (workers=%d)...", total, _INGEST_WORKERS)
    hashed: list[tuple[Path, Path, str, str]] = []  # (path, src_zip, content_id, real_ext)
    computed = 0
    with ThreadPoolExecutor(max_workers=_INGEST_WORKERS) as pool:
        future_map = {
            pool.submit(_hash_and_detect, path): (path, src_zip)
            for path, src_zip in media_files
        }
        for future in as_completed(future_map):
            path, src_zip = future_map[future]
            computed += 1
            if progress:
                progress(computed, total, path.name)
            try:
                content_id, real_ext = future.result()
            except Exception as exc:
                log.warning("雜湊計算失敗 (%s): %s — 使用備援方式", path.name, exc)
                content_id = _sha256(path)
                real_ext = path.suffix.lower().lstrip(".")
            hashed.append((path, src_zip, content_id, real_ext))

    # ── Phase 2b: Sequential DB writes ──────────────────────────────────────────────
    ingested = 0
    duplicates = 0
    source_zips_str = ", ".join(str(z) for z in zips)
    with transaction(db_path) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(run_id, source_zip, extracted_root, status) VALUES (?, ?, ?, 'DONE')",
            (run_id, source_zips_str, str(extract_root)),
        )
        for path, src_zip, content_id, real_ext in hashed:
            sidecar = _find_sidecar(path)
            row = conn.execute("SELECT content_id, extracted_path, patch_status FROM media_items WHERE content_id=?", (content_id,)).fetchone()
            if row:
                # File already in DB. Check if its extracted_path is stale (file missing).
                old_path = Path(row['extracted_path']) if row['extracted_path'] else None
                if old_path is None or not old_path.exists():
                    # PURGED items should NOT be updated — they are already uploaded.
                    if row['patch_status'] == 'PURGED':
                        duplicates += 1
                        conn.execute(
                            "INSERT INTO duplicates(content_id, dup_path, source_zip, ingest_run_id) VALUES (?, ?, ?, ?)",
                            (content_id, str(path), str(src_zip), run_id),
                        )
                        log.info("跳過已 PURGED 項目: %s", path.name)
                    else:
                        # Not yet uploaded — update to the newly-extracted location.
                        conn.execute(
                            "UPDATE media_items SET extracted_path=?, sidecar_path=?, has_sidecar=?, updated_at=CURRENT_TIMESTAMP WHERE content_id=?",
                            (str(path), str(sidecar) if sidecar else None, 1 if sidecar else 0, content_id),
                        )
                        log.info("更新遗失檔案路徑: %s", path.name)
                        ingested += 1  # Count as restored
                else:
                    duplicates += 1
                    conn.execute(
                        "INSERT INTO duplicates(content_id, dup_path, source_zip, ingest_run_id) VALUES (?, ?, ?, ?)",
                        (content_id, str(path), str(src_zip), run_id),
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
                    real_ext,
                    str(src_zip),
                    str(path),
                    str(sidecar) if sidecar else None,
                    1 if sidecar else 0,
                    run_id,
                ),
            )
            ingested += 1

    log.info("匯入完成: scanned=%d, ingested=%d, duplicates=%d", total, ingested, duplicates)
    return {
        "run_id": run_id,
        "extract_root": str(extract_root),
        "zip_count": len(zips),
        "scanned": total,
        "ingested": ingested,
        "duplicates": duplicates,
    }


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


def cmd_reconcile(db_path: Path, progress: ProgressCallback = None) -> dict:
    import bisect
    matched = 0
    parsed = 0
    log.info("開始比對 sidecar")

    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT id, extracted_path, sidecar_path, has_sidecar FROM media_items WHERE patch_status='NEW'"
        ).fetchall()
        total = len(rows)
        log.info("共 %d 筆待比對", total)

        # ── Phase 1: Pre-scan directories ─────────────────────────────────────
        # For each directory, read all *.json files ONCE and build two indices:
        #   by_dir_norm:  dir → { normalized_stem → json_path }   O(1) lookup
        #   by_dir_title: dir → ( [sorted_title_stems], [paths] ) O(log n) bisect
        dir_set = {str(Path(r["extracted_path"]).parent) for r in rows}
        log.info("正在預載 %d 個目錄的 JSON 索引...", len(dir_set))

        by_dir_norm: dict[str, dict[str, Path]] = {}
        by_dir_title: dict[str, tuple[list[str], list[Path]]] = {}

        for d in dir_set:
            norm_map: dict[str, Path] = {}
            title_list: list[tuple[str, Path]] = []
            for json_path in Path(d).glob("*.json"):
                # Build normalized-stem index (first match wins)
                jnorm = _normalize_stem(json_path.name)
                norm_map.setdefault(jnorm, json_path)
                # Read JSON once for title index
                try:
                    data = json.loads(json_path.read_text(encoding="utf-8"))
                    title = data.get("title", "")
                    if title:
                        title_list.append((Path(title).stem.lower(), json_path))
                except (json.JSONDecodeError, OSError):
                    pass
            by_dir_norm[d] = norm_map
            # Sort by title stem for binary-search prefix matching
            title_list.sort(key=lambda x: x[0])
            by_dir_title[d] = (
                [t[0] for t in title_list],  # stems (sorted)
                [t[1] for t in title_list],  # paths (parallel list)
            )

        # ── Phase 2: Match each media file ────────────────────────────────────
        sidecar_updates: list[tuple[str, int]] = []  # (sidecar_path, id)
        meta_updates: list[tuple] = []               # (epoch, lat, lng, id)

        for i, r in enumerate(rows, 1):
            if progress:
                progress(i, total, Path(r["extracted_path"]).name)

            media_path = Path(r["extracted_path"])
            dir_key = str(media_path.parent)

            if not r["has_sidecar"]:
                norm = _normalize_stem(media_path.name)
                candidate = None

                # 1. O(1) normalized-stem dict lookup
                candidate = by_dir_norm.get(dir_key, {}).get(norm)

                # 2. Direct filename check (.jpg.json / .supplemental-metadata.json)
                if not candidate:
                    candidate = _find_sidecar(media_path)

                # 3. O(log n) title prefix search via bisect (replaces O(n) disk reads)
                if not candidate:
                    media_stem = media_path.stem.lower()
                    if len(media_stem) >= 10:
                        t_stems, t_paths = by_dir_title.get(dir_key, ([], []))
                        idx = bisect.bisect_left(t_stems, media_stem)
                        if idx < len(t_stems) and t_stems[idx].startswith(media_stem):
                            candidate = t_paths[idx]

                if candidate:
                    sidecar_updates.append((str(candidate), r["id"]))
                    matched += 1
                    sidecar = candidate
                else:
                    # No sidecar found yet — keep as NEW
                    # (JSON may arrive in a later ZIP import)
                    continue
            else:
                sidecar = Path(r["sidecar_path"])

            if sidecar.exists():
                ts, lat, lng = _parse_sidecar(sidecar)
                meta_updates.append((ts, lat, lng, r["id"]))
                parsed += 1

        # ── Phase 3: Batch DB writes ───────────────────────────────────────────
        if sidecar_updates:
            conn.executemany(
                "UPDATE media_items SET sidecar_path=?, has_sidecar=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                sidecar_updates,
            )
        if meta_updates:
            conn.executemany(
                """
                UPDATE media_items
                SET expected_taken_epoch=?, expected_lat=?, expected_lng=?,
                    patch_status=CASE WHEN patch_status='NEW' THEN 'READY' ELSE patch_status END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
                """,
                meta_updates,
            )

    log.info("比對完成: matched=%d, parsed=%d", matched, parsed)
    return {"matched_sidecar": matched, "parsed": parsed}


def _run(cmd: list[str]) -> tuple[int, str, str]:
    p = subprocess.run(cmd, capture_output=True, text=True)
    stdout = p.stdout.strip() if p.stdout else ""
    stderr = p.stderr.strip() if p.stderr else ""
    return p.returncode, stdout, stderr


def _verify_patch(
    exiftool_bin: str, dst: Path,
    expected_epoch: int | None,
    expected_lat: float | None, expected_lng: float | None,
) -> str | None:
    """Read back metadata from *dst* and compare against expectations.

    Always uses a fresh ``exiftool`` subprocess (not the session) so it can
    never desync the session state on timeout.
    Returns ``None`` on success or a human-readable diff string on mismatch.
    """
    read_args = ["-j", "-n",
                 "-DateTimeOriginal", "-GPSLatitude", "-GPSLongitude",
                 str(dst)]
    # Always use _run for read-back: avoids session desync on timeout/error
    rc, stdout, _ = _run([exiftool_bin] + read_args)
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
                diff = abs(actual_epoch - expected_epoch)
                # Allow ±2s precision and timezone offset differences (multiples of 3600, up to ±14h)
                if diff > 2 and not (diff % 3600 <= 2 and diff <= 50400):
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
    BATCH_SIZE = 50

    # ── Try to start ExifTool -stay_open session (10–30x faster than per-process) ──
    session: ExifToolSession | None = None
    try:
        session = ExifToolSession(exiftool_bin)
        log.info("ExifTool -stay_open session 已啟動 (pid=%d)", session._proc.pid)
    except Exception as exc:
        log.warning("無法啟動 ExifTool session，退回單次呼叫模式: %s", exc)

    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id, content_id, ext, extracted_path, expected_taken_epoch, expected_lat, expected_lng FROM media_items WHERE patch_status='READY'"
        ).fetchall()
        total = len(rows)
        log.info("共 %d 筆待 patch", total)
        for idx, r in enumerate(rows, 1):
            if progress:
                progress(idx, total, Path(r["extracted_path"]).name)
            src = Path(r["extracted_path"])

            if not src.exists():
                err = f"source file missing: {src}"
                log.warning("❌ 原始檔案遗失: %s", src)
                conn.execute(
                    "UPDATE media_items SET patch_status='FAILED', patch_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (err, r["id"]),
                )
                fail += 1
                if idx % BATCH_SIZE == 0:
                    conn.commit()
                continue

            # Detect actual format (handles .PNG files that are really JPEG)
            try:
                real_ext = _detect_real_ext(src)
            except Exception as e:
                log.warning("無法偵測檔案格式: %s (%s)", src, e)
                real_ext = r["ext"]

            ext = real_ext if real_ext != r["ext"] else r["ext"]
            if ext != r["ext"]:
                log.info("副檔名修正: %s (.%s → .%s)", src.name, r['ext'], ext)
            dst = patched_dir / f"{r['content_id']}.{ext}"
            shutil.copy2(src, dst)
            err = ""
            is_image = f".{ext}" in IMAGE_EXTS

            if is_image:
                # ── Image patch via ExifTool ──
                et_args = ["-overwrite_original", "-m"]
                if r["expected_taken_epoch"]:
                    ts = datetime.fromtimestamp(r["expected_taken_epoch"], tz=timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
                    et_args += [f"-DateTimeOriginal={ts}"]
                if r["expected_lat"] is not None and r["expected_lng"] is not None:
                    lat, lng = r["expected_lat"], r["expected_lng"]
                    et_args += [
                        f"-GPSLatitude={abs(lat)}",
                        f"-GPSLatitudeRef={'N' if lat >= 0 else 'S'}",
                        f"-GPSLongitude={abs(lng)}",
                        f"-GPSLongitudeRef={'E' if lng >= 0 else 'W'}",
                    ]
                et_args += [str(dst)]

                if session is not None:
                    # ── Fast path: reuse running ExifTool process ──
                    try:
                        rc, _ = session.execute(et_args)
                    except TimeoutError as exc:
                        log.warning("ExifTool session 進時 (%s)，重新啟動 session...", exc)
                        try:
                            session.close()
                        except Exception:
                            pass
                        try:
                            session = ExifToolSession(exiftool_bin)
                            log.info("ExifTool session 重啟成功 (pid=%d)", session._proc.pid)
                        except Exception as exc2:
                            log.warning("重啟失敗，退回單次呼叫模式: %s", exc2)
                            session = None
                        rc, _, _ = _run([exiftool_bin] + et_args)  # fallback for this file
                    except Exception as exc:
                        log.warning("ExifTool session 錯誤，退回單次呼叫: %s", exc)
                        session = None
                        rc, _, _ = _run([exiftool_bin] + et_args)
                else:
                    # ── Fallback: spawn per-file process ──
                    rc, _, _ = _run([exiftool_bin] + et_args)

                if rc != 0:
                    log.warning("ExifTool 回傅非零 (%d)，將以驗證結果為準: %s", rc, src.name)
            else:
                # ── Video patch via ffmpeg (unchanged) ──
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

            # ── Post-patch verification (images only) ──
            if is_image:
                verify_err = _verify_patch(
                    exiftool_bin, dst,
                    r["expected_taken_epoch"], r["expected_lat"], r["expected_lng"],
                )
                if verify_err:
                    if not err:
                        err = f"verify-failed: {verify_err}"
                else:
                    err = ""   # verification passed — clear any ExifTool warnings
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
            # Commit in batches to avoid losing all progress on crash
            if idx % BATCH_SIZE == 0:
                conn.commit()
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        if session is not None:
            session.close()
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
    selected: list[tuple[str, Path, int, str]] = []
    total_bytes = 0
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT content_id, patched_path, original_name FROM media_items WHERE patch_status='PATCHED' AND batch_id IS NULL ORDER BY created_at"
        ).fetchall()
        for r in rows:
            p = Path(r["patched_path"])
            if not p.exists():
                continue
            size = p.stat().st_size
            if len(selected) >= max_files or total_bytes + size > max_bytes:
                break
            # Store original_name for later usage
            selected.append((r["content_id"], p, size, r["original_name"]))
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
        
        # Track used filenames in this batch to handle collisions
        used_filenames = set()

        with manifest_csv.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["batch_id", "content_id", "file_name", "file_size", "source_patched_path"])
            
            for content_id, src, size, orig_name in selected:
                # Resolve filename collision
                candidate = orig_name
                stem = Path(candidate).stem
                suffix = Path(candidate).suffix
                counter = 1
                while candidate.lower() in used_filenames:
                    candidate = f"{stem}_{counter}{suffix}"
                    counter += 1
                
                used_filenames.add(candidate.lower())
                dst = files_dir / candidate
                
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
    passed_count = 0
    sample = []
    with transaction(db_path) as conn:
        row = conn.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise MigError(f"batch not found: {batch_id}")
        files_dir = Path(row["local_batch_path"]) / "files"
        if not files_dir.exists():
            raise MigError(f"batch files missing: {files_dir}")
        
        # User wants to push *contents* directly to Camera, preventing a 'files' subdir.
        # Adding 'u\.' (e.g. 'files\.') tells adb to push contents.
        src_arg = str(files_dir)
        if not src_arg.endswith(os.sep):
            src_arg += os.sep
        src_arg += "."
        
        log.info("正在推送檔案至裝置: %s ...", device_path)
        rc, _, stderr = _run([adb_bin, "push", src_arg, device_path])
        if rc != 0:
            raise MigError(f"adb push failed: {stderr}")

        # --- Phase 1: Automated Verification (ADB) ---
        b_items = conn.execute("SELECT file_name FROM batch_items WHERE batch_id=?", (batch_id,)).fetchall()
        passed_count = 0
        sample: list = []
        if not b_items:
            log.warning("批次無檔案，跳過驗證")
        else:
            # Use plain `ls` to get filenames only — no size parsing, no find.
            # Works on all Android versions with toybox or busybox.
            rc_ls, ls_out, ls_err = _run([adb_bin, "shell", "ls", device_path])
            if rc_ls != 0 or not ls_out.strip():
                log.warning("自動驗證已跳過 (adb ls 失敗: %s)", ls_err.strip() or ls_out.strip() or "no output")
            else:
                remote_names: set[str] = set(ls_out.splitlines())
                sample = random.sample(b_items, k=min(len(b_items), 50))
                for item in sample:
                    if item["file_name"] in remote_names:
                        passed_count += 1
                    else:
                        log.warning("自動驗證失敗 (Missing): %s", item["file_name"])
                log.info("自動驗證結果: %d/%d 通過", passed_count, len(sample))


        conn.execute(
            "UPDATE batches SET pushed_at=?, status='PUSHED', device_target_path=? WHERE batch_id=?",
            (now_iso(), device_path, batch_id),
        )
    log.info("批次 %s 推送完成 -> %s", batch_id, device_path)
    return {"batch_id": batch_id, "device_path": device_path, "verify_passed": passed_count, "verify_total": len(sample)}


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


def cmd_purge(root: Path, db_path: Path, batch_id: str) -> dict:
    with transaction(db_path) as conn:
        row = conn.execute("SELECT status, local_batch_path FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise MigError(f"batch not found: {batch_id}")
        if row["status"] != "VERIFIED":
            raise MigError("purge is only allowed for VERIFIED batch")

        # 1. Delete batch files directory
        files_dir = Path(row["local_batch_path"]) / "files"
        if files_dir.exists():
            shutil.rmtree(files_dir)

        # 2. Delete patched files + extracted originals + sidecars
        items = conn.execute(
            "SELECT extracted_path, sidecar_path, patched_path FROM media_items WHERE batch_id=?",
            (batch_id,),
        ).fetchall()

        removed_patched = 0
        removed_extracted = 0
        removed_sidecar = 0

        for it in items:
            # Delete patched file
            if it["patched_path"]:
                p = Path(it["patched_path"])
                if p.exists():
                    p.unlink()
                    removed_patched += 1

            # Delete original extracted file
            if it["extracted_path"]:
                p = Path(it["extracted_path"])
                if p.exists():
                    p.unlink()
                    removed_extracted += 1

            # Delete sidecar JSON
            if it["sidecar_path"]:
                p = Path(it["sidecar_path"])
                if p.exists():
                    p.unlink()
                    removed_sidecar += 1

        conn.execute(
            "UPDATE media_items SET patched_path=NULL, patch_status='PURGED' WHERE batch_id=?",
            (batch_id,),
        )
        conn.execute("UPDATE batches SET status='PURGED', purged_at=? WHERE batch_id=?", (now_iso(), batch_id))

    log.info(
        "清除完成: batch=%s, patched=%d, extracted=%d, sidecar=%d",
        batch_id, removed_patched, removed_extracted, removed_sidecar,
    )
    return {
        "batch_id": batch_id,
        "removed_patched": removed_patched,
        "removed_extracted": removed_extracted,
        "removed_sidecar": removed_sidecar,
    }


def cmd_clean_duplicates(db_path: Path) -> dict:
    """Clean up duplicate files recorded in the database.

    SAFETY: Only removes files that are NOT referenced in media_items.
    Files are MOVED to a 'duplicates_trash' folder, not permanently deleted.
    """


    with transaction(db_path) as conn:
        # Get all paths referenced by media_items (these must NEVER be deleted)
        protected_paths: set[str] = set()
        for row in conn.execute(
            "SELECT extracted_path FROM media_items WHERE extracted_path IS NOT NULL"
        ).fetchall():
            protected_paths.add(row["extracted_path"])

        rows = conn.execute("SELECT id, dup_path FROM duplicates").fetchall()

        # Create trash directory next to state.db  →  data/work/duplicates_trash
        trash_dir = Path(db_path).parent.parent / "work" / "duplicates_trash"
        trash_dir.mkdir(parents=True, exist_ok=True)

        moved_count = 0
        skipped_protected = 0
        already_gone = 0
        space_freed = 0

        for row in rows:
            dup_path = row["dup_path"]
            path = Path(dup_path)

            # CRITICAL: Skip if this path is also in media_items
            if dup_path in protected_paths:
                skipped_protected += 1
                continue

            if not path.exists():
                already_gone += 1
                continue

            try:
                size = path.stat().st_size
                dest = trash_dir / path.name
                if dest.exists():
                    dest = trash_dir / f"{path.stem}_{row['id']}{path.suffix}"
                shutil.move(str(path), str(dest))
                moved_count += 1
                space_freed += size
            except Exception as exc:
                log.warning("無法移動檔案 %s: %s", path, exc)

    log.info(
        "清理重複檔案: moved=%d, skipped_protected=%d, already_gone=%d, freed=%.2f MB",
        moved_count, skipped_protected, already_gone, space_freed / (1024 * 1024),
    )
    return {
        "moved_count": moved_count,
        "skipped_protected": skipped_protected,
        "already_gone": already_gone,
        "space_freed_mb": round(space_freed / (1024 * 1024), 2),
        "trash_dir": str(trash_dir),
    }




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


def get_batch_samples(db_path: Path, batch_id: str, count: int = 5) -> list[str]:
    with transaction(db_path) as conn:
        rows = conn.execute(
            "SELECT file_name FROM batch_items WHERE batch_id=? ORDER BY RANDOM() LIMIT ?",
            (batch_id, count),
        ).fetchall()
        return [r["file_name"] for r in rows]


def cmd_mark_verified(db_path: Path, batch_id: str) -> dict:
    """Mark a batch as VERIFIED after user confirmation via UI."""
    with transaction(db_path) as conn:
        row = conn.execute("SELECT status FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
        if not row:
            raise MigError(f"batch not found: {batch_id}")
        if row["status"] != "PUSHED":
            raise MigError(f"batch status must be PUSHED, current: {row['status']}")
        
        conn.execute("UPDATE batches SET status='VERIFIED', verified_at=? WHERE batch_id=?", (now_iso(), batch_id))
    
    log.info("批次 %s 已標記為 VERIFIED", batch_id)
    return {"batch_id": batch_id, "status": "VERIFIED"}
