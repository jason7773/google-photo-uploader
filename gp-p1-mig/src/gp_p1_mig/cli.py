from __future__ import annotations

from pathlib import Path

import typer
from rich import print

from .workflow import (
    MigError,
    cmd_export_verify,
    cmd_import_verify,
    cmd_ingest,
    cmd_init,
    cmd_make_batch,
    cmd_patch,
    cmd_purge,
    cmd_push,
    cmd_reconcile,
    default_db,
)

app = typer.Typer(help="Google Photos Takeout -> Pixel 1 migration CLI")


def _root(path: Path | None) -> Path:
    return (path or Path.cwd()).resolve()


def _db(root: Path, db_path: Path | None) -> Path:
    return (db_path or default_db(root)).resolve()


def _run(fn):
    try:
        out = fn()
        print(f"[green]OK[/green] {out}")
    except MigError as e:
        print(f"[red]ERROR[/red] {e}")
        raise typer.Exit(code=2)
    except Exception as e:
        print(f"[red]UNEXPECTED[/red] {e}")
        raise typer.Exit(code=1)


@app.command("init")
def init_cmd(root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_init(r, d))


@app.command("ingest")
def ingest_cmd(zip_path: Path = typer.Argument(...), root: Path = typer.Option(None), db: Path = typer.Option(None), run_id: str = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_ingest(r, d, zip_path.resolve(), run_id=run_id))


@app.command("reconcile")
def reconcile_cmd(root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_reconcile(d))


@app.command("patch")
def patch_cmd(root: Path = typer.Option(None), db: Path = typer.Option(None), exiftool_bin: str = typer.Option("exiftool"), ffmpeg_bin: str = typer.Option("ffmpeg")):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_patch(r, d, exiftool_bin=exiftool_bin, ffmpeg_bin=ffmpeg_bin))


@app.command("make-batch")
def make_batch_cmd(max_bytes: int = typer.Option(10 * 1024 * 1024 * 1024), max_files: int = typer.Option(1000), root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_make_batch(r, d, max_bytes=max_bytes, max_files=max_files))


@app.command("push")
def push_cmd(batch_id: str = typer.Argument(...), device_path: str = typer.Option("/sdcard/DCIM/Camera"), adb_bin: str = typer.Option("adb"), root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_push(r, d, batch_id=batch_id, device_path=device_path, adb_bin=adb_bin))


@app.command("export-verify")
def export_verify_cmd(batch_id: str = typer.Argument(...), sample_size: int = typer.Option(30), root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_export_verify(r, d, batch_id=batch_id, sample_size=sample_size))


@app.command("import-verify")
def import_verify_cmd(batch_id: str = typer.Argument(...), result_csv: Path = typer.Argument(...), root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_import_verify(d, batch_id=batch_id, result_csv=result_csv.resolve()))


@app.command("purge")
def purge_cmd(batch_id: str = typer.Argument(...), purge_patched: bool = typer.Option(False), root: Path = typer.Option(None), db: Path = typer.Option(None)):
    r = _root(root)
    d = _db(r, db)
    _run(lambda: cmd_purge(r, d, batch_id=batch_id, purge_patched=purge_patched))


if __name__ == "__main__":
    app()
