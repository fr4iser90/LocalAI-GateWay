"""OnPrem AI Gateway."""

from __future__ import annotations

from pathlib import Path


def _read_version() -> str:
    for candidate in (
        Path(__file__).resolve().parent.parent / "VERSION",
        Path("/app/VERSION"),
    ):
        try:
            text = candidate.read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            continue
    return "0.0.0-dev"


__version__ = _read_version()
