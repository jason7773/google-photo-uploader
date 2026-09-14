"""Launch the local Web UI — opens browser automatically."""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import webbrowser
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def _setup_logging(workspace: Path):
    """Set up persistent file logging in data/logs/."""
    log_dir = workspace / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    log_file = log_dir / f"app_{datetime.now().strftime('%Y%m%d')}.log"

    file_handler = TimedRotatingFileHandler(
        log_file,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s | %(levelname)-5s | %(name)s | %(message)s")
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.addHandler(file_handler)

    logging.info("Log 檔案位置: %s", log_file)


def main():
    parser = argparse.ArgumentParser(description="本機 Google Photos Takeout 遷移工具")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    from .settings import load_settings
    settings = load_settings()
    _setup_logging(Path(settings["root"]))

    try:
        from .web.app import start
    except ImportError as e:
        print(f"缺少依賴套件: {e}")
        print("請先安裝: pip install flask flask-socketio")
        sys.exit(1)

    port = args.port
    url = f"http://127.0.0.1:{port}"
    print(f"啟動本地 Web UI: {url}")
    if not args.no_browser:
        timer = threading.Timer(1.5, lambda: webbrowser.open(url))
        timer.daemon = True
        timer.start()
    start(port=port)


if __name__ == "__main__":
    main()
