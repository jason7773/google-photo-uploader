"""ExifTool ``-stay_open`` session — high-performance batch metadata writing.

Instead of spawning a new ``exiftool`` process per file (0.2-0.5 s each),
this class starts ExifTool once and reuses it via the ``-stay_open True -@ -``
protocol.  For 5,000 images with 2 ExifTool calls each this saves ~1,000–5,000 s.

Protocol overview
-----------------
1. Launch:  exiftool -stay_open True -@ -
2. Per command:
   - Write args one per line to stdin, terminate with ``-execute``
   - Read stdout until the ``{ready}`` (or ``{readyN}``) sentinel appears
3. Shutdown: write ``-stay_open\\nFalse`` to stdin

Timeout safety
--------------
A background daemon thread continuously reads stdout into a ``queue.Queue``.
``execute()`` pops from that queue with a configurable ``timeout`` so the
caller is never blocked forever by a hung or slow ExifTool invocation.
If a timeout fires, the session is in an inconsistent state and **must** be
closed; the caller should restart it or fall back to per-process mode.
"""
from __future__ import annotations

import logging
import queue
import re
import subprocess
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Matches the {ready} or {readyN} sentinel that ExifTool prints after each command.
_READY_RE = re.compile(r'^\{ready(\d*)\}\s*$')

# Default timeout (seconds) for a single ExifTool command.
DEFAULT_TIMEOUT = 60.0


class ExifToolSession:
    """Keep one ExifTool process alive across many metadata write/read commands.

    Uses a background daemon thread to read stdout so that ``execute()`` can
    apply a wall-clock timeout and never block the main thread indefinitely.

    Usage::

        with ExifToolSession(exiftool_bin) as session:
            rc, out = session.execute(["-overwrite_original", "-m",
                                       "-DateTimeOriginal=2023:01:01 12:00:00",
                                       "/path/to/file.jpg"])

    If ExifTool cannot be started (old version, bad path, etc.) the constructor
    raises ``RuntimeError`` and the caller should fall back to per-process mode.

    If ``execute()`` raises ``TimeoutError``, the session is corrupt; close it
    and start a new one (or fall back to per-process) for remaining files.
    """

    def __init__(self, exiftool_bin: str) -> None:
        log.debug("Starting ExifTool session: %s", exiftool_bin)
        self._bin = exiftool_bin
        self._proc = subprocess.Popen(
            [exiftool_bin, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,   # captured but not per-command delimited
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"ExifTool process failed to start immediately: {exiftool_bin}"
            )

        # Background daemon thread: reads stdout → queue so we can timeout.
        self._q: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name="exiftool-stdout-reader")
        self._reader.start()

        log.debug("ExifTool -stay_open session ready (pid=%d)", self._proc.pid)

    # ── Background reader ─────────────────────────────────────────────────────

    def _read_loop(self) -> None:
        """Daemon thread: pump stdout lines into ``self._q``.

        Puts ``None`` into the queue on EOF so ``execute()`` can detect
        that the process has died.
        """
        try:
            assert self._proc.stdout is not None
            while True:
                line = self._proc.stdout.readline()
                if not line:          # EOF — process ended
                    self._q.put(None)
                    return
                self._q.put(line)
        except Exception:
            self._q.put(None)

    # ── Public API ────────────────────────────────────────────────────────────

    def execute(self, args: list[str], timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
        """Send *args* to ExifTool and return ``(rc, stdout_text)``.

        *args* must NOT include the exiftool binary itself.  They are forwarded
        as-is, one per stdin line, followed by ``-execute``.

        Raises
        ------
        TimeoutError
            ExifTool did not print ``{ready}`` within *timeout* seconds.
            The session is now in an inconsistent state — close and restart it.
        RuntimeError
            ExifTool stdout was closed (process crashed).
        """
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"ExifTool session (pid={self._proc.pid}) has already terminated"
            )

        # Write command to stdin
        payload = "\n".join(args) + "\n-execute\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

        # Read stdout from queue until {ready} sentinel or timeout
        out_parts: list[str] = []
        rc = 0
        deadline = time.monotonic() + timeout

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(
                    f"ExifTool did not respond within {timeout:.0f}s "
                    f"(last arg: {args[-1] if args else '?'})"
                )
            try:
                line = self._q.get(timeout=remaining)
            except queue.Empty:
                raise TimeoutError(
                    f"ExifTool did not respond within {timeout:.0f}s "
                    f"(last arg: {args[-1] if args else '?'})"
                )

            if line is None:  # EOF — process died
                raise RuntimeError(
                    "ExifTool stdout closed unexpectedly — process may have crashed"
                )

            m = _READY_RE.match(line)
            if m:
                suffix = m.group(1)
                rc = int(suffix) if suffix else 0
                break
            out_parts.append(line)

        return rc, "".join(out_parts)

    def close(self) -> None:
        """Gracefully shut down the ExifTool session."""
        try:
            if self._proc.poll() is None:
                assert self._proc.stdin is not None
                self._proc.stdin.write("-stay_open\nFalse\n")
                self._proc.stdin.flush()
                self._proc.wait(timeout=10)
                log.debug("ExifTool session closed gracefully (pid=%d)", self._proc.pid)
        except Exception as exc:
            log.debug("ExifTool session close error — killing process: %s", exc)
            try:
                self._proc.kill()
            except Exception:
                pass

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self) -> "ExifToolSession":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
