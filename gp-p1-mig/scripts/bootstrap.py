"""Create an isolated local environment, then start the app without touching data."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import sys
import venv


def main() -> int:
    if sys.version_info < (3, 11):
        print("Python 3.11 or newer is required.")
        return 1
    project = Path(__file__).resolve().parents[1]
    environment = project / ".venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    fingerprint = hashlib.sha256((project / "pyproject.toml").read_bytes() +
                                 (project / "requirements-lock.txt").read_bytes() +
                                 str(project).encode() + sys.version.encode()).hexdigest()
    stamp = environment / ".gp-p1-mig-installed"
    if not python.exists():
        print("Creating local Python environment...", flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    if not stamp.exists() or stamp.read_text() != fingerprint:
        print("Installing application dependencies (internet required on first launch)...", flush=True)
        subprocess.run([str(python), "-m", "pip", "install", "-r", str(project / "requirements-lock.txt")], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "--no-deps", "--no-build-isolation", "-e", str(project)], check=True)
        stamp.write_text(fingerprint)
    if "--setup-only" in sys.argv[1:]:
        print("Setup complete. Run start.bat to open the application.")
        return 0
    # The source checkout is the stable default; never infer a new root from launch CWD.
    return subprocess.call([str(python), "-m", "gp_p1_mig.web_ui", *sys.argv[1:]], cwd=project)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"Setup failed: {exc}\nCheck internet access and retry. Existing data is preserved.")
        raise SystemExit(1)
