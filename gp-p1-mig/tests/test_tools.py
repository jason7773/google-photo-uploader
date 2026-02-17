"""Tests for tools.py — tool path discovery."""
from __future__ import annotations

from gp_p1_mig.tools import verify_tools


def test_verify_tools_returns_dict():
    result = verify_tools()
    assert "exiftool" in result
    assert "ffmpeg" in result
    for name in result:
        assert "path" in result[name]
        assert "version" in result[name]
