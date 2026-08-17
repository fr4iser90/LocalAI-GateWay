"""Upstream preflight before proxying (optional, fail-open)."""

from __future__ import annotations

from dataclasses import dataclass

from .data.source_load import load_cache, probe_source_load


@dataclass(frozen=True)
class PreflightResult:
    ok: bool
    reason: str = ""
    retry_after: int = 15


def preflight_upstream(
    *,
    backend: str,
    kind: str,
    model: str | None = None,
    timeout: float = 2.0,
) -> PreflightResult:
    """Return not-ok when backend reports loading or (chat) model not loaded.

    Network errors and unknown engines → ok (fail open).
    """
    addr = (backend or "").strip()
    if not addr:
        return PreflightResult(ok=True)

    snap = probe_source_load(backend=addr, kind=kind, model=model, timeout=timeout)
    load_cache.put(addr, kind, model, snap)

    if snap.state == "loading":
        return PreflightResult(False, "model_initializing", 15)
    if snap.state == "busy":
        return PreflightResult(False, "backend_busy", 5)
    if snap.state == "down":
        return PreflightResult(ok=True)
    if kind == "chat" and snap.model_loaded is False:
        return PreflightResult(False, "model_initializing", 15)
    return PreflightResult(ok=True)
