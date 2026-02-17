"""Launch the local Web UI — opens browser automatically."""
from __future__ import annotations

import sys
import threading
import webbrowser


def main():
    try:
        from .web.app import start
    except ImportError as e:
        print(f"缺少依賴套件: {e}")
        print("請先安裝: pip install flask flask-socketio")
        sys.exit(1)

    port = 5000
    url = f"http://127.0.0.1:{port}"
    print(f"啟動本地 Web UI: {url}")
    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    start(port=port)


if __name__ == "__main__":
    main()
