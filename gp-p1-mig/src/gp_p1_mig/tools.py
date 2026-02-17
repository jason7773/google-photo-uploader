"""External tool path discovery: tools/ folder → system PATH → error."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

class ToolNotFoundError(RuntimeError):
    """Raised when a required external tool cannot be located."""

# Candidate locations relative to the project root.
# When running from source: gp-p1-mig/tools/
# When running from PyInstaller bundle: _MEIPASS/tools/ or beside the exe.
_TOOL_SEARCH_DIRS: list[Path] = [
    Path(__file__).resolve().parents[2] / "tools",   # source layout: gp-p1-mig/tools/
    Path(__file__).resolve().parent / "tools",        # bundled layout
]


def _find_bin(name: str, extra_dirs: list[Path] | None = None) -> str:
    """Return the absolute path to *name* (.exe on Windows).

    Search order:
    1. ``extra_dirs`` (caller-supplied overrides)
    2. ``_TOOL_SEARCH_DIRS`` — first directly, then **recursively** in
       subdirectories (handles versioned folders like ``exiftool-13.50_64/``).
    3. System ``PATH`` via :func:`shutil.which`
    """
    # Patterns to match: "exiftool.exe", "exiftool(-k).exe", plain "exiftool"
    exe_names = [f"{name}.exe", f"{name}(-k).exe", name]

    search_roots = (extra_dirs or []) + _TOOL_SEARCH_DIRS
    for root_dir in search_roots:
        if not root_dir.is_dir():
            continue
        # Direct match in root_dir
        for exe in exe_names:
            p = root_dir / exe
            if p.is_file():
                return str(p)
        # Recursive search (max 3 levels deep)
        for exe in exe_names:
            for match in root_dir.rglob(exe):
                if match.is_file():
                    return str(match)

    system = shutil.which(name)
    if system:
        return system

    raise ToolNotFoundError(
        f"找不到 {name}。請將 {name} 完整資料夾放入 tools/ 資料夾，"
        f"或確認已加入系統 PATH。"
    )


def find_exiftool(extra_dirs: list[Path] | None = None) -> str:
    path = _find_bin("exiftool", extra_dirs)
    p = Path(path)
    # exiftool(-k).exe is the "press-any-key" version that hangs when
    # called without a file argument.  Create a renamed copy to avoid this.
    if "(-k)" in p.name:
        renamed = p.with_name(p.name.replace("(-k)", ""))
        if not renamed.exists():
            shutil.copy2(p, renamed)
        return str(renamed)
    return path


def find_ffmpeg(extra_dirs: list[Path] | None = None) -> str:
    return _find_bin("ffmpeg", extra_dirs)


def find_adb(extra_dirs: list[Path] | None = None) -> str:
    return _find_bin("adb", extra_dirs)


def _version(bin_path: str) -> str:
    try:
        p = subprocess.run(
            [bin_path, "-ver"] if "exiftool" in bin_path.lower() else [bin_path, "-version"],
            capture_output=True, text=True, timeout=10)
        return p.stdout.strip().split("\n")[0] if p.returncode == 0 else "unknown"
    except Exception:
        return "unknown"


def verify_tools(extra_dirs: list[Path] | None = None) -> dict[str, dict[str, str]]:
    """Return paths and versions for exiftool and ffmpeg."""
    result: dict[str, dict[str, str]] = {}
    for name, finder in [("exiftool", find_exiftool), ("ffmpeg", find_ffmpeg), ("adb", find_adb)]:
        try:
            path = finder(extra_dirs)
            result[name] = {"path": path, "version": _version(path)}
        except ToolNotFoundError:
            result[name] = {"path": "NOT FOUND", "version": "N/A"}
    return result
