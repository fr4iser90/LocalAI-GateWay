"""Best-effort upstream probes (llama health/slots, ollama, generic /health)."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from sqlalchemy.orm import Session

from ..config import MODEL_CHECK_KINDS
from .backends import list_sources
from .models import BackendSource

_TIMEOUT = 2.0
_loading_since: dict[str, float] = {}


@dataclass
class ServiceStatus:
    service: str
    backend: str
    state: str = "down"
    detail: str = ""
    kind: str = ""
    slots_total: int | None = None
    slots_busy: int | None = None
    slots_idle: int | None = None
    models: list[str] = field(default_factory=list)
    probes_ok: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _base_url(backend: str) -> str:
    return f"http://{backend}"


def _get(client: httpx.Client, url: str) -> httpx.Response | None:
    try:
        return client.get(url)
    except Exception:
        return None


def _health_state(resp: httpx.Response) -> tuple[str, str] | None:
    if resp.status_code >= 500:
        return None
    if 200 <= resp.status_code < 500:
        try:
            data = resp.json()
            if isinstance(data, dict):
                status = str(data.get("status") or data.get("state") or "").lower()
                if status in {"ok", "healthy", "ready"}:
                    return "ok", status or "ok"
                if status in {"loading", "starting"}:
                    return "loading", status
        except Exception:
            pass
        if resp.status_code == 200:
            return "ok", f"HTTP {resp.status_code}"
        return "unknown", f"HTTP {resp.status_code}"
    return None


def _parse_slots(data: Any) -> tuple[int, int, int] | None:
    if not isinstance(data, list):
        return None
    total = len(data)
    busy = 0
    for slot in data:
        if isinstance(slot, dict) and slot.get("is_processing"):
            busy += 1
    return total, busy, total - busy


def probe_source(src: BackendSource) -> ServiceStatus:
    service = src.name
    kind = src.kind
    backend = (src.address or "").strip()
    if not backend:
        return ServiceStatus(
            service=service, backend="", kind=kind, state="unset", detail="not configured"
        )

    key = f"{service}:{backend}"
    base = _base_url(backend)
    status = ServiceStatus(
        service=service, backend=backend, kind=kind, state="down", detail="unreachable"
    )

    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
            health_paths = ["/health", "/v1/health"]
            if kind == "chat":
                health_paths = ["/health", "/v1/health", "/api/tags"]

            for path in health_paths:
                resp = _get(client, base + path)
                if resp is None:
                    continue
                parsed = _health_state(resp)
                if parsed is None:
                    continue
                state, detail = parsed
                status.state = state
                status.detail = detail
                status.probes_ok.append(path)
                if state != "down":
                    break

            if status.state == "down":
                resp = _get(client, base + "/")
                if resp is not None:
                    status.state = "unknown"
                    status.detail = f"reachable (HTTP {resp.status_code})"
                    status.probes_ok.append("/")

            if kind in ("chat", "embed") and status.state != "down":
                resp = _get(client, base + "/slots")
                if resp is not None and resp.status_code == 200:
                    try:
                        data = resp.json()
                    except Exception:
                        data = None
                    parsed_slots = _parse_slots(data)
                    if parsed_slots:
                        total, busy, idle = parsed_slots
                        status.slots_total = total
                        status.slots_busy = busy
                        status.slots_idle = idle
                        status.probes_ok.append("/slots")
                        if busy and status.state == "ok":
                            status.state = "busy"
                            status.detail = f"{busy}/{total} slots busy"
                        elif status.state == "ok":
                            status.detail = f"{total} slots, all idle"

            if kind == "chat" and status.state != "down" and not status.models:
                resp = _get(client, base + "/api/ps")
                if resp is not None and resp.status_code == 200:
                    try:
                        data = resp.json()
                        models = []
                        for m in data.get("models") or []:
                            name = m.get("name") or m.get("model")
                            if name:
                                models.append(str(name))
                        status.models = models
                        status.probes_ok.append("/api/ps")
                        if models:
                            status.detail = f"{len(models)} model(s) loaded"
                    except Exception:
                        pass
                if not status.models:
                    tags = _get(client, base + "/api/tags")
                    if tags is not None and tags.status_code == 200:
                        try:
                            data = tags.json()
                            status.models = [
                                str(m.get("name"))
                                for m in (data.get("models") or [])
                                if m.get("name")
                            ][:12]
                            status.probes_ok.append("/api/tags")
                        except Exception:
                            pass

            if kind in MODEL_CHECK_KINDS | {"stt"} and status.state not in ("down", "unset"):
                if "/v1/models" not in status.probes_ok:
                    resp = _get(client, base + "/v1/models")
                    if resp is not None and resp.status_code == 200:
                        try:
                            data = resp.json()
                            ids = [
                                str(m.get("id"))
                                for m in (data.get("data") or [])
                                if isinstance(m, dict) and m.get("id")
                            ]
                            if ids and not status.models:
                                status.models = ids[:12]
                            status.probes_ok.append("/v1/models")
                        except Exception:
                            pass

            if status.state == "loading":
                _loading_since.setdefault(key, time.time())
            else:
                _loading_since.pop(key, None)

    except Exception as exc:  # noqa: BLE001
        status.state = "down"
        status.detail = str(exc)[:120]

    return status


def probe_all(db: Session) -> list[ServiceStatus]:
    sources = list_sources(db)
    if not sources:
        return []
    results: dict[str, ServiceStatus] = {}
    with ThreadPoolExecutor(max_workers=max(4, len(sources))) as pool:
        futs = {pool.submit(probe_source, s): s.name for s in sources}
        for fut in as_completed(futs):
            st = fut.result()
            results[st.service] = st
    return [results[s.name] for s in sources if s.name in results]
