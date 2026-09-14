"""Flask application — local-only Web UI for gp-p1-mig."""
from __future__ import annotations

import logging
import threading
import secrets
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

from ..settings import load_settings, save_settings
from ..db import connect, init_db
from ..tools import find_adb, verify_tools
from ..workflow import (
    MigError,
    cmd_export_verify,
    cmd_import_verify,
    cmd_ingest,
    cmd_init,
    cmd_make_batch,
    cmd_mark_verified,
    cmd_patch,
    cmd_purge,
    preview_purge,
    cmd_push,
    cmd_reconcile,
    cmd_retry_failed,
    cmd_clean_duplicates,
    default_db,
    ensure_workspace,
)

log = logging.getLogger(__name__)

# ── Flask + SocketIO setup ──

import os
import threading

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(24).hex()
app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "[::1]"]
app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
API_TOKEN = secrets.token_urlsafe(32)
socketio = SocketIO(app, async_mode="threading")

# ── Global state (single-user local app) ──

STATE = {
    **load_settings(),
    "busy": False,
    "current_task": None,
}


# ── Custom logging handler → push logs to browser ──

class SocketIOLogHandler(logging.Handler):
    def emit(self, record):
        try:
            msg = self.format(record)
            socketio.emit("log", {"level": record.levelname, "message": msg})
        except Exception:
            pass


_sio_handler = SocketIOLogHandler()
_sio_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S"))
logging.getLogger("gp_p1_mig").addHandler(_sio_handler)
logging.getLogger("gp_p1_mig").setLevel(logging.INFO)


# ── Helpers ──

def _root() -> Path:
    return Path(STATE["root"]).resolve()


def _db() -> Path:
    return Path(STATE["db"]).resolve()


def _progress_callback(current: int, total: int, desc: str) -> None:
    socketio.emit("progress", {"current": current, "total": total, "desc": desc})


_state_lock = threading.Lock()


def _run_task(name: str, fn, *args, **kwargs):
    """Run a workflow function in a background thread, emitting results via SocketIO."""
    with _state_lock:
        if STATE["busy"]:
            from werkzeug.exceptions import Conflict
            raise Conflict("另一個任務正在執行中，請稍候。")
        STATE["busy"] = True
        STATE["current_task"] = name

    def _worker():
        socketio.emit("task_start", {"name": name})
        try:
            result = fn(*args, **kwargs)
            socketio.emit("task_done", {"name": name, "result": result})
        except MigError as e:
            socketio.emit("task_error", {"name": name, "error": str(e)})
        except Exception as e:
            socketio.emit("task_error", {"name": name, "error": str(e)})
        finally:
            with _state_lock:
                STATE["busy"] = False
                STATE["current_task"] = None
            socketio.emit("progress", {"current": 0, "total": 0, "desc": ""})

    threading.Thread(target=_worker, daemon=True).start()


@app.before_request
def protect_local_api():
    origin = request.headers.get("Origin")
    if origin and origin != request.host_url.rstrip("/"):
        return jsonify(ok=False, error="拒絕跨來源操作。"), 403
    if request.method == "POST" and request.path.startswith("/api/"):
        token = request.headers.get("X-App-Token", "")
        if not secrets.compare_digest(token, API_TOKEN):
            return jsonify(ok=False, error="操作憑證已失效，請重新整理網頁。"), 403
        if not request.is_json or not isinstance(request.get_json(silent=True), dict):
            return jsonify(ok=False, error="請提供 JSON 物件。"), 400


@app.errorhandler(400)
@app.errorhandler(409)
def api_request_error(error):
    return jsonify(ok=False, error=error.description), error.code


# ── Pages ──

@app.route("/")
def index():
    return render_template("index.html", api_token=API_TOKEN)


# ── REST API ──

@app.route("/api/state")
def api_state():
    """Return current system state + DB stats."""
    r, d = _root(), _db()
    stats = {"total": 0, "pending": 0, "patched": 0, "batched": 0, "uploaded": 0, "failed": 0}
    batches = []
    tools = {}
    try:
        tools = verify_tools()
    except Exception:
        pass
    conn = None
    db_error = None
    try:
        if not d.exists():
            raise FileNotFoundError("尚未初始化工作區，請按 Init。")
        conn = connect(d)

        # ── Single GROUP BY replaces 6 separate COUNT(*) queries ──────────────
        # Each row: patch_status, cnt (total for status), no_batch (PATCHED with no batch)
        counts: dict[str, int] = {}
        no_batch: dict[str, int] = {}
        for row in conn.execute(
            """
            SELECT patch_status,
                   COUNT(*) AS cnt,
                   SUM(CASE WHEN batch_id IS NULL THEN 1 ELSE 0 END) AS no_batch
            FROM media_items
            GROUP BY patch_status
            """
        ).fetchall():
            counts[row["patch_status"]] = row["cnt"]
            no_batch[row["patch_status"]] = row["no_batch"]

        stats["total"]     = sum(counts.values())
        stats["new_count"] = counts.get("NEW", 0)
        stats["ready"]     = counts.get("READY", 0)
        stats["pending"]   = stats["new_count"] + stats["ready"]
        stats["patched"]   = no_batch.get("PATCHED", 0)   # PATCHED with no batch_id
        stats["uploaded"]  = counts.get("PURGED", 0)
        stats["failed"]    = counts.get("FAILED", 0)

        # Batched = items assigned to batches not yet VERIFIED/PURGED
        row_b = conn.execute(
            """
            SELECT COUNT(*) FROM media_items m
            JOIN batches b ON m.batch_id = b.batch_id
            WHERE b.status NOT IN ('VERIFIED', 'PURGED')
            """
        ).fetchone()
        stats["batched"] = row_b[0] if row_b else 0

        for row in conn.execute(
            "SELECT batch_id, status, total_files, total_bytes, created_at FROM batches ORDER BY created_at DESC"
        ).fetchall():
            batches.append(dict(row))
    except Exception as exc:
        db_error = str(exc)
    finally:
        if conn is not None:
            conn.close()
    return jsonify({
        "root": STATE["root"],
        "db": STATE["db"],
        "busy": STATE["busy"],
        "current_task": STATE["current_task"],
        "stats": stats,
        "batches": batches,
        "tools": tools,
        "db_error": db_error,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    with _state_lock:
        if STATE["busy"]:
            return jsonify(ok=False, error="任務執行中不能切換工作區。"), 409
        raw_root = data.get("root", STATE["root"])
        if not isinstance(raw_root, str) or not raw_root.strip():
            return jsonify(ok=False, error="請填寫工作區絕對路徑。"), 400
        root = Path(raw_root).expanduser()
        if not root.is_absolute():
            return jsonify(ok=False, error="工作區必須使用絕對路徑。"), 400
        root = root.resolve()
        raw_db = data.get("db", "")
        if not raw_db or (root != _root() and raw_db == STATE["db"]):
            db = default_db(root)
        else:
            db = Path(raw_db).expanduser()
            if not db.is_absolute():
                db = root / db
        db = db.resolve()
        if root.exists() and not root.is_dir():
            return jsonify(ok=False, error="工作區必須是資料夾。"), 400
        save_settings(root, db)
        STATE.update(root=str(root), db=str(db))
    return jsonify(ok=True, root=str(root), db=str(db))


@app.route("/api/init", methods=["POST"])
def api_init():
    _run_task("Init", cmd_init, _root(), _db())
    return jsonify({"ok": True})


@app.route("/api/ingest", methods=["POST"])
def api_ingest():
    data = request.json or {}
    # Support both: zip_paths (list) and legacy zip_path (single string)
    raw_paths = data.get("zip_paths") or []
    if not raw_paths:
        single = data.get("zip_path", "")
        if single:
            raw_paths = [single]
    if not raw_paths:
        return jsonify({"ok": False, "error": "請指定至少一個 ZIP 路徑"}), 400
    resolved = [Path(p).resolve() for p in raw_paths]
    _run_task("Ingest", cmd_ingest, _root(), _db(), zip_paths=resolved, progress=_progress_callback)
    return jsonify({"ok": True})


@app.route("/api/reconcile", methods=["POST"])
def api_reconcile():
    _run_task("Reconcile", cmd_reconcile, _db(), progress=_progress_callback)
    return jsonify({"ok": True})


@app.route("/api/patch", methods=["POST"])
def api_patch():
    _run_task("Patch", cmd_patch, _root(), _db(), progress=_progress_callback)
    return jsonify({"ok": True})


@app.route("/api/retry-failed", methods=["POST"])
def api_retry_failed():
    _run_task("Retry Failed", cmd_retry_failed, _root(), _db(), progress=_progress_callback)
    return jsonify({"ok": True})


@app.route("/api/make-batch", methods=["POST"])
def api_make_batch():
    data = request.json or {}
    max_files = int(data.get("max_files", 1000))
    max_bytes = int(data.get("max_bytes", 10 * 1024 * 1024 * 1024))
    _run_task("Make Batch", cmd_make_batch, _root(), _db(), max_bytes=max_bytes, max_files=max_files)
    return jsonify({"ok": True})


@app.route("/api/push", methods=["POST"])
def api_push():
    data = request.json or {}
    batch_id = data.get("batch_id", "")
    device_path = data.get("device_path", "/sdcard/DCIM/Camera")
    if not batch_id:
        return jsonify({"ok": False, "error": "請指定 Batch ID"}), 400

    try:
        adb_bin = find_adb()
    except Exception:
        return jsonify({"ok": False, "error": "找不到 ADB 工具。請安裝 ADB 並加入 PATH，或是將 adb.exe 放入 tools/ 資料夾。"}), 500

    _run_task("Push", cmd_push, _root(), _db(), batch_id=batch_id, device_path=device_path, adb_bin=adb_bin)
    return jsonify({"ok": True})


@app.route("/api/export-verify", methods=["POST"])
def api_export_verify():
    data = request.json or {}
    batch_id = data.get("batch_id", "")
    if not batch_id:
        return jsonify({"ok": False, "error": "請指定 Batch ID"}), 400
    _run_task("Export Verify", cmd_export_verify, _root(), _db(), batch_id=batch_id)
    return jsonify({"ok": True})


@app.route("/api/import-verify", methods=["POST"])
def api_import_verify():
    data = request.json or {}
    batch_id = data.get("batch_id", "")
    csv_path = data.get("csv_path", "")
    if not batch_id or not csv_path:
        return jsonify({"ok": False, "error": "請指定 Batch ID 與 CSV 路徑"}), 400
    _run_task("Import Verify", cmd_import_verify, _db(), batch_id=batch_id, result_csv=Path(csv_path))
    return jsonify({"ok": True})


@app.route("/api/purge-preview")
def api_purge_preview():
    try:
        return jsonify(ok=True, plan=preview_purge(_root(), _db(), request.args.get("batch_id", "")))
    except MigError as exc:
        return jsonify(ok=False, error=str(exc)), 400


@app.route("/api/purge", methods=["POST"])
def api_purge():
    data = request.json or {}
    batch_id = data.get("batch_id", "")
    if not batch_id:
        return jsonify({"ok": False, "error": "請指定 Batch ID"}), 400
    _run_task("Purge", cmd_purge, _root(), _db(), batch_id=batch_id)
    return jsonify({"ok": True})


@app.route("/api/mark-verified", methods=["POST"])
def api_mark_verified():
    data = request.json or {}
    batch_id = data.get("batch_id", "")
    if not batch_id:
        return jsonify({"ok": False, "error": "請指定 Batch ID"}), 400
    _run_task("Mark Verified", cmd_mark_verified, _db(), batch_id)
    return jsonify({"ok": True})


@app.route("/api/clean-duplicates", methods=["POST"])
def api_clean_duplicates():
    _run_task("Clean Duplicates", cmd_clean_duplicates, _db())
    return jsonify(ok=True)


@app.route("/api/batch-samples", methods=["GET"])
def api_batch_samples():
    batch_id = request.args.get("batch_id", "")
    if not batch_id:
        return jsonify({"ok": False, "error": "請指定 Batch ID"}), 400
    try:
        from ..workflow import get_batch_samples
        samples = get_batch_samples(_db(), batch_id)
        return jsonify({"ok": True, "samples": samples})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── SocketIO events ──

@socketio.on("connect")
def on_connect(auth=None):
    if not auth or not secrets.compare_digest(str(auth.get("token", "")), API_TOKEN):
        return False
    log.info("瀏覽器已連線")


def start(host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    """Start the local web server."""
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
