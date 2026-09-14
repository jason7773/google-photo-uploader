"""Local workspace selection; never relocate or reset an existing database."""
from __future__ import annotations

import json
import os
from pathlib import Path


def default_root() -> Path:
    if os.environ.get("GP_P1_MIG_ROOT"):
        return Path(os.environ["GP_P1_MIG_ROOT"]).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").is_file():
        return source_root
    # Keep CLI users' existing workspaces usable after a wheel installation.
    if (Path.cwd() / "data/state/state.db").is_file():
        return Path.cwd().resolve()
    return (Path.home() / "gp-p1-mig-workspace").resolve()


def settings_path() -> Path:
    return default_root() / "data/settings.json"


def load_settings() -> dict[str, str]:
    root = default_root()
    result = {"root": str(root), "db": str(root / "data/state/state.db")}
    path = settings_path()
    if path.exists():
        try:
            saved = json.loads(path.read_text(encoding="utf-8"))
            for key in result:
                if not isinstance(saved.get(key), str) or not Path(saved[key]).is_absolute():
                    raise ValueError("root/db must be absolute paths")
            result.update({key: saved[key] for key in result})
        except (ValueError, TypeError) as exc:
            raise ValueError(f"設定檔無法讀取，請修正 {path}；不會自動切換資料庫。") from exc
    return result


def save_settings(root: Path, db: Path) -> None:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps({"root": str(root.resolve()), "db": str(db.resolve())},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
