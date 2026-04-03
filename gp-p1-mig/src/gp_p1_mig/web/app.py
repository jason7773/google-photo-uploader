"""Flask application — local-only Web UI for gp-p1-mig."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO

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
socketio = SocketIO(app, async_mode="threading")

# ── Global state (single-user local app) ──

STATE = {
    "root": str(Path.cwd().resolve()),
    "db": str(default_db(Path.cwd().resolve())),
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
            socketio.emit("error", {"message": "另一個任務正在執行中，請稍候。"})
            return
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


# ── Pages ──

@app.route("/")
def index():
    return render_template("index.html")


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
    try:
        conn = connect(d)
        
        # 1. Total count (all items)
        stats["total"] = conn.execute("SELECT COUNT(*) FROM media_items").fetchone()[0]
        
        # 2a. NEW (no sidecar yet)
        stats["new_count"] = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE patch_status='NEW'"
        ).fetchone()[0]
        
        # 2b. READY (sidecar matched, waiting for patch)
        stats["ready"] = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE patch_status='READY'"
        ).fetchone()[0]
        
        # 2c. Pending total (backwards compat)
        stats["pending"] = stats["new_count"] + stats["ready"]
        
        # 3. Patched (PATCHED but NOT BATCHED)
        stats["patched"] = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE patch_status='PATCHED' AND batch_id IS NULL"
        ).fetchone()[0]
        
        # 4. Batched (In batches but not yet verified/purged)
        stats["batched"] = conn.execute(
            """
            SELECT COUNT(*) FROM media_items m
            JOIN batches b ON m.batch_id = b.batch_id
            WHERE b.status NOT IN ('VERIFIED', 'PURGED')
            """
        ).fetchone()[0]
        
        # 5. Uploaded (items with patch_status=PURGED — includes batch-purged + manually marked)
        stats["uploaded"] = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE patch_status='PURGED'"
        ).fetchone()[0]
        
        # 6. Failed
        stats["failed"] = conn.execute(
            "SELECT COUNT(*) FROM media_items WHERE patch_status='FAILED'"
        ).fetchone()[0]
        
        for row in conn.execute(
            "SELECT batch_id, status, total_files, total_bytes, created_at FROM batches ORDER BY created_at DESC"
        ).fetchall():
            batches.append(dict(row))
        conn.close()
    except Exception:
        pass
    return jsonify({
        "root": STATE["root"],
        "db": STATE["db"],
        "busy": STATE["busy"],
        "current_task": STATE["current_task"],
        "stats": stats,
        "batches": batches,
        "tools": tools,
    })


@app.route("/api/settings", methods=["POST"])
def api_settings():
    data = request.json or {}
    if "root" in data:
        STATE["root"] = str(Path(data["root"]).resolve())
    if "db" in data:
        STATE["db"] = str(Path(data["db"]).resolve())
    return jsonify({"ok": True, "root": STATE["root"], "db": STATE["db"]})


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
    try:
        stats = cmd_clean_duplicates(Path(_db()))
        return jsonify({"ok": True, "stats": stats})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


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
def on_connect():
    log.info("瀏覽器已連線")


def start(host: str = "127.0.0.1", port: int = 5000, debug: bool = False):
    """Start the local web server."""
    socketio.run(app, host=host, port=port, debug=debug, allow_unsafe_werkzeug=True)
