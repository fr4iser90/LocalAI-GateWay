"""Cached upstream load snapshots for routing among tied sources."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import httpx

from .backends import BackendSource

_CACHE_TTL_SEC = 3.0
_PROBE_TIMEOUT = 1.5

# Lower sort key = preferred for routing.
_STATE_RANK = {
    "ok": 0,
    "busy": 1,
    "loading": 2,
    "unknown": 3,
    "down": 4,
}


@dataclass(frozen=True)
class SourceLoadSnapshot:
    state: str = "unknown"
    slots_total: int | None = None
    slots_idle: int | None = None
    model_loaded: bool | None = None
    probed_at: float = 0.0


def _ollama_loaded(model: str, loaded: list[str]) -> bool:
    if not loaded:
        return False
    want = model.strip()
    if not want:
        return True
    base = want.split(":", 1)[0]
    for name in loaded:
        n = name.strip()
        if n == want or n.startswith(want + ":") or n == base or n.startswith(base + ":"):
            return True
    return False


def _parse_slots(data: object) -> tuple[int, int, int] | None:
    if not isinstance(data, list):
        return None
    total = len(data)
    busy = sum(
        1 for slot in data if isinstance(slot, dict) and slot.get("is_processing")
    )
    return total, busy, total - busy


def _health_state(resp: httpx.Response) -> str | None:
    if resp.status_code >= 500:
        return None
    try:
        data = resp.json()
        if isinstance(data, dict):
            status = str(data.get("status") or data.get("state") or "").lower()
            if status in {"ok", "healthy", "ready"}:
                return "ok"
            if status in {"loading", "starting"}:
                return "loading"
    except Exception:
        pass
    if 200 <= resp.status_code < 500:
        return "ok"
    return None


def probe_source_load(
    *,
    backend: str,
    kind: str,
    model: str | None = None,
    timeout: float = _PROBE_TIMEOUT,
) -> SourceLoadSnapshot:
    """Best-effort load probe (≤ timeout). Unreachable → state=down."""
    addr = (backend or "").strip()
    if not addr:
        return SourceLoadSnapshot(state="down", probed_at=time.time())

    base = f"http://{addr}"
    state = "unknown"
    slots_total: int | None = None
    slots_idle: int | None = None
    model_loaded: bool | None = None
    now = time.time()

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            for path in ("/health", "/v1/health", "/api/tags"):
                resp = client.get(base + path)
                if resp is None or resp.status_code >= 500:
                    continue
                parsed = _health_state(resp)
                if parsed == "loading":
                    return SourceLoadSnapshot(
                        state="loading", probed_at=now
                    )
                if parsed == "ok":
                    state = "ok"
                    break
                if resp.status_code == 200:
                    state = "ok"
                    break

            if kind in {"chat", "embed"}:
                slots_resp = client.get(base + "/slots")
                if slots_resp.status_code == 200:
                    parsed = _parse_slots(slots_resp.json())
                    if parsed:
                        slots_total, _busy, slots_idle = parsed
                        if slots_total > 0 and slots_idle <= 0:
                            state = "busy"

            if kind == "chat" and (model or "").strip():
                ps = client.get(base + "/api/ps")
                if ps.status_code == 200:
                    try:
                        data = ps.json()
                    except Exception:
                        data = {}
                    names: list[str] = []
                    if isinstance(data, dict):
                        for row in data.get("models") or []:
                            if isinstance(row, dict):
                                n = row.get("name") or row.get("model")
                                if n:
                                    names.append(str(n))
                    if names:
                        model_loaded = _ollama_loaded(model or "", names)
                        if model_loaded is False and state == "ok":
                            state = "loading"
    except Exception:
        return SourceLoadSnapshot(state="down", probed_at=now)

    return SourceLoadSnapshot(
        state=state,
        slots_total=slots_total,
        slots_idle=slots_idle,
        model_loaded=model_loaded,
        probed_at=now,
    )


def load_sort_key(snapshot: SourceLoadSnapshot | None) -> tuple:
    """Lower = less loaded / preferred."""
    if snapshot is None:
        return (_STATE_RANK["unknown"], 1, 0)
    state = _STATE_RANK.get(snapshot.state, _STATE_RANK["unknown"])
    loaded_penalty = 0
    if snapshot.model_loaded is False:
        loaded_penalty = 1
    idle = snapshot.slots_idle
    if idle is None:
        idle_score = 0
    else:
        idle_score = -idle
    return (state, loaded_penalty, idle_score)


class LoadCache:
    def __init__(self, ttl_sec: float = _CACHE_TTL_SEC) -> None:
        self._ttl = ttl_sec
        self._lock = threading.Lock()
        self._entries: dict[str, SourceLoadSnapshot] = {}

    @staticmethod
    def _key(backend: str, kind: str, model: str | None) -> str:
        return f"{backend}|{kind}|{(model or '').strip()}"

    def get(
        self, backend: str, kind: str, model: str | None = None
    ) -> SourceLoadSnapshot | None:
        key = self._key(backend, kind, model)
        with self._lock:
            snap = self._entries.get(key)
            if snap is None:
                return None
            if time.time() - snap.probed_at > self._ttl:
                del self._entries[key]
                return None
            return snap

    def put(
        self,
        backend: str,
        kind: str,
        model: str | None,
        snapshot: SourceLoadSnapshot,
    ) -> None:
        key = self._key(backend, kind, model)
        with self._lock:
            self._entries[key] = snapshot

    def snapshot_for(
        self,
        src: BackendSource,
        *,
        kind: str,
        model: str | None = None,
    ) -> SourceLoadSnapshot:
        backend = (src.address or "").strip()
        cached = self.get(backend, kind, model)
        if cached is not None:
            return cached
        snap = probe_source_load(backend=backend, kind=kind, model=model)
        self.put(backend, kind, model, snap)
        return snap


load_cache = LoadCache()
