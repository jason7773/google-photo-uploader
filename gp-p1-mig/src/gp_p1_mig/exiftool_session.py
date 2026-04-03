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
"""
from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# Matches the {ready} or {readyN} sentinel that ExifTool prints after each command.
_READY_RE = re.compile(r'^\{ready(\d*)\}\s*$')


class ExifToolSession:
    """Keep one ExifTool process alive across many metadata write/read commands.

    Usage::

        with ExifToolSession(exiftool_bin) as session:
            rc, out = session.execute(["-overwrite_original", "-m",
                                       "-DateTimeOriginal=2023:01:01 12:00:00",
                                       "/path/to/file.jpg"])

    If ExifTool cannot be started (old version, bad path, etc.) the constructor
    raises ``RuntimeError`` and the caller should fall back to per-process mode.
    """

    def __init__(self, exiftool_bin: str) -> None:
        log.debug("Starting ExifTool session: %s", exiftool_bin)
        self._bin = exiftool_bin
        self._proc = subprocess.Popen(
            [exiftool_bin, "-stay_open", "True", "-@", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,   # capture separately; not per-command delimited
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"ExifTool process failed to start immediately: {exiftool_bin}"
            )
        log.debug("ExifTool -stay_open session ready (pid=%d)", self._proc.pid)

    # ── Public API ────────────────────────────────────────────────────────────

    def execute(self, args: list[str]) -> tuple[int, str]:
        """Send *args* to ExifTool and return ``(rc, stdout_text)``.

        *args* must NOT include the exiftool binary itself (the session already
        has a running process).  They are forwarded as-is, one per stdin line,
        followed by ``-execute``.

        ``rc`` is 0 on success.  Many ExifTool versions always emit ``{ready}``
        even on minor errors; use the ``_verify_patch`` round-trip to confirm
        metadata was actually written correctly.
        """
        if self._proc.poll() is not None:
            raise RuntimeError(
                f"ExifTool session (pid={self._proc.pid}) terminated unexpectedly"
            )

        # Build the stdin payload: one arg per line, terminated by -execute
        payload = "\n".join(args) + "\n-execute\n"
        assert self._proc.stdin is not None
        self._proc.stdin.write(payload)
        self._proc.stdin.flush()

        # Read stdout until the {ready} sentinel
        assert self._proc.stdout is not None
        out_parts: list[str] = []
        rc = 0
        while True:
            line = self._proc.stdout.readline()
            if not line:
                # EOF — ExifTool crashed or was killed
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
                log.debug("ExifTool session closed (pid=%d)", self._proc.pid)
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
