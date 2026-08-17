from __future__ import annotations

import re

from sqlalchemy.orm import Session

from ..config import (
    KINDS,
    SOURCE_NAME_RE,
    Settings,
    public_route_for_source,
)
from .models import BackendConfig, BackendSource

# host or host:port (IPv4 / hostname); empty allowed
_BACKEND_RE = re.compile(
    r"^("
    r"(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)"
    r"|(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)*[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    r")"
    r"(?::(?:[1-9]\d{0,4}|0))?"
    r"$"
)


def list_sources(db: Session) -> list[BackendSource]:
    return (
        db.query(BackendSource)
        .order_by(BackendSource.kind, BackendSource.is_default.desc(), BackendSource.name)
        .all()
    )


def source_names(db: Session) -> list[str]:
    return [s.name for s in list_sources(db)]


def default_grant_source_names(db: Session) -> list[str]:
    """Safe defaults for new users/teams: every source except isolated (lab/heavy)."""
    return [s.name for s in list_sources(db) if not s.isolated]


def source_chip_rows(
    db: Session, names: list[str] | None = None
) -> list[dict]:
    """UI rows for source checkboxes: name, badges, one-line hint."""
    wanted = set(names) if names is not None else None
    rows: list[dict] = []
    for s in list_sources(db):
        if wanted is not None and s.name not in wanted:
            continue
        hint = ""
        if s.kind == "chat":
            if s.is_default:
                hint = "Daily · /v1 merge"
            elif s.isolated:
                hint = f"Lab / heavy · /s/{s.name}/ only"
            else:
                hint = "Extra chat · merge when granted"
        elif s.isolated:
            hint = f"Separate · /s/{s.name}/ only"
        elif s.is_default:
            hint = f"Default {s.kind}"
        rows.append(
            {
                "name": s.name,
                "kind": s.kind,
                "address": s.address or "",
                "is_default": bool(s.is_default),
                "isolated": bool(s.isolated),
                "hint": hint,
            }
        )
    return rows


def get_source_by_name(db: Session, name: str) -> BackendSource | None:
    return db.query(BackendSource).filter(BackendSource.name == name.lower()).first()


def default_source_for_kind(db: Session, kind: str) -> BackendSource | None:
    row = (
        db.query(BackendSource)
        .filter(BackendSource.kind == kind, BackendSource.is_default.is_(True))
        .first()
    )
    if row:
        return row
    return (
        db.query(BackendSource)
        .filter(BackendSource.kind == kind)
        .order_by(BackendSource.name)
        .first()
    )


def parse_route_models(raw: str) -> list[str]:
    out: list[str] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        out.append(line)
    return out


def normalize_route_models(raw: str) -> str:
    return "\n".join(parse_route_models(raw))


def catalog_route_models(db: Session, source_name: str) -> list[str]:
    """Enabled catalog model ids for a source (auto merge targets after Models → Sync)."""
    from .models import CatalogModel

    rows = (
        db.query(CatalogModel.model_id)
        .filter(
            CatalogModel.source_name == source_name,
            CatalogModel.enabled.is_(True),
        )
        .order_by(CatalogModel.model_id)
        .all()
    )
    return [r[0] for r in rows]


def route_patterns_for_source(db: Session, src: BackendSource) -> list[str]:
    """Enabled catalog model ids for this source (Models → Sync)."""
    return catalog_route_models(db, src.name)


def model_match_score(pattern: str, model: str) -> int | None:
    """Higher = better. None = no match."""
    if not pattern or not model:
        return None
    if pattern == model:
        return 1000 + len(pattern)
    if pattern.endswith("*") and len(pattern) > 1:
        prefix = pattern[:-1]
        if model.startswith(prefix):
            return 500 + len(prefix)
        return None
    # bare name matches name and name:tag (Ollama-style)
    if ":" not in pattern and (model == pattern or model.startswith(pattern + ":")):
        return 400 + len(pattern)
    return None


def resolve_source_for_kind(
    db: Session,
    kind: str,
    *,
    model: str | None = None,
) -> BackendSource | None:
    """Pick upstream for a default-path (/v1) request — merge by model.

    Sources with ``isolated=True`` never win merge (use /s/{name}/ instead).
    Patterns = enabled catalog models for the source (after Models → Sync).
    Unmatched / missing model → non-isolated default of that kind.
    """
    sources = (
        db.query(BackendSource)
        .filter(BackendSource.kind == kind)
        .order_by(BackendSource.name)
        .all()
    )
    if not sources:
        return None

    if model:
        ranked: list[tuple[int, int, str, BackendSource]] = []
        for src in sources:
            if src.isolated:
                continue
            patterns = route_patterns_for_source(db, src)
            if not patterns:
                continue
            best_score = None
            for pat in patterns:
                s = model_match_score(pat, model)
                if s is not None and (best_score is None or s > best_score):
                    best_score = s
            if best_score is None:
                continue
            ranked.append((best_score, 0 if not src.is_default else 1, src.name, src))
        if ranked:
            ranked.sort(key=lambda t: (-t[0], t[1], t[2]))
            return ranked[0][3]

    # Default fallback: prefer non-isolated default
    for src in sources:
        if src.is_default and not src.isolated:
            return src
    for src in sources:
        if not src.isolated:
            return src
    # All isolated — still need something for /v1 (first default or first)
    return default_source_for_kind(db, kind)


def address_for_source(db: Session, name: str) -> str:
    src = get_source_by_name(db, name)
    return (src.address if src else "").strip()


def normalize_backend(raw: str) -> str:
    return (raw or "").strip()


def validate_backend(raw: str) -> str | None:
    """Return error message or None if OK. Empty is allowed (source disabled)."""
    value = normalize_backend(raw)
    if not value:
        return None
    if not _BACKEND_RE.match(value):
        return "Use host or host:port (e.g. 192.168.1.10:8080 or localai:8080)"
    if ":" in value:
        port = int(value.rsplit(":", 1)[-1])
        if port < 1 or port > 65535:
            return "Port must be 1–65535"
    return None


def validate_source_name(name: str) -> str | None:
    n = (name or "").strip().lower()
    if not n:
        return "Name is required"
    if not SOURCE_NAME_RE.match(n):
        return "Name: start with a–z, then a–z / 0–9 / _ / - (max 32)"
    if n == "s":
        return "Name 's' is reserved"
    return None


def validate_kind(kind: str) -> str | None:
    if kind not in KINDS:
        return f"Kind must be one of: {', '.join(KINDS)}"
    return None


def clear_default_for_kind(db: Session, kind: str, *, except_id: int | None = None) -> None:
    q = db.query(BackendSource).filter(
        BackendSource.kind == kind, BackendSource.is_default.is_(True)
    )
    if except_id is not None:
        q = q.filter(BackendSource.id != except_id)
    for row in q.all():
        row.is_default = False


def upsert_source(
    db: Session,
    *,
    name: str,
    kind: str,
    address: str,
    is_default: bool,
    route_models: str = "",
    isolated: bool = False,
    api_style: str = "auto",
    gpu_power_url: str = "",
) -> BackendSource:
    from ..config import API_STYLES

    name = name.strip().lower()
    address = normalize_backend(address)
    models = normalize_route_models(route_models)
    style = (api_style or "auto").strip().lower()
    if style not in API_STYLES:
        style = "auto"
    gpu = (gpu_power_url or "").strip()
    existing = get_source_by_name(db, name)
    if is_default:
        clear_default_for_kind(db, kind, except_id=existing.id if existing else None)
    if existing:
        existing.kind = kind
        existing.address = address
        existing.is_default = is_default
        existing.route_models = models
        existing.isolated = isolated
        existing.api_style = style
        existing.gpu_power_url = gpu
        return existing
    # If this is the first of its kind, force default
    siblings = db.query(BackendSource).filter(BackendSource.kind == kind).count()
    if siblings == 0:
        is_default = True
    src = BackendSource(
        name=name,
        kind=kind,
        address=address,
        is_default=is_default,
        route_models=models,
        isolated=isolated,
        api_style=style,
        gpu_power_url=gpu,
    )
    db.add(src)
    db.flush()
    return src


def delete_source(db: Session, src: BackendSource) -> None:
    kind = src.kind
    was_default = src.is_default
    db.delete(src)
    db.flush()
    if was_default:
        nxt = (
            db.query(BackendSource)
            .filter(BackendSource.kind == kind)
            .order_by(BackendSource.name)
            .first()
        )
        if nxt:
            nxt.is_default = True


def migrate_backend_config_to_sources(db: Session) -> None:
    """One-shot: copy legacy backend_config columns into backend_sources."""
    if db.query(BackendSource).count() > 0:
        return
    cfg = db.query(BackendConfig).first()
    if cfg is None:
        return
    mapping = [
        ("chat", "chat", (cfg.chat or "").strip(), True),
        ("chat2", "chat", (getattr(cfg, "chat2", None) or "").strip(), False),
        ("embed", "embed", (cfg.embed or "").strip(), True),
        ("stt", "stt", (cfg.stt or "").strip(), True),
        ("tts", "tts", (cfg.tts or "").strip(), True),
    ]
    for name, kind, address, is_default in mapping:
        if not address:
            continue
        upsert_source(db, name=name, kind=kind, address=address, is_default=is_default)


def seed_backends_from_env(db: Session, settings: Settings) -> None:
    """Migrate legacy config, then fill missing sources from env."""
    migrate_backend_config_to_sources(db)

    seeds: list[tuple[str, str, str, bool]] = [
        (
            "chat",
            "chat",
            settings.chat_source
            or settings.chat_backend
            or settings.llm_backend
            or settings.ollama_backend,
            True,
        ),
        ("embed", "embed", settings.embed_source or settings.embed_backend, True),
        ("stt", "stt", settings.stt_source or settings.stt_backend, True),
        ("tts", "tts", settings.tts_source or settings.tts_backend, True),
        ("chat2", "chat", settings.chat2_source or settings.chat2_backend, False),
    ]
    for name, kind, address, is_default in seeds:
        address = normalize_backend(address)
        if not address:
            continue
        existing = get_source_by_name(db, name)
        if existing:
            if not existing.address:
                existing.address = address
            continue
        # Don't steal default if kind already has one
        has_default = (
            db.query(BackendSource)
            .filter(BackendSource.kind == kind, BackendSource.is_default.is_(True))
            .first()
            is not None
        )
        upsert_source(
            db,
            name=name,
            kind=kind,
            address=address,
            is_default=is_default and not has_default,
        )


def source_rows(
    db: Session, settings: Settings | None = None
) -> list[tuple[BackendSource, str]]:
    """(source, public_route_display)."""
    rows = []
    for src in list_sources(db):
        route = public_route_for_source(
            src.name, src.kind, is_default=src.is_default, settings=settings
        )
        rows.append((src, route))
    return rows


# --- deprecated helpers (thin wrappers) ---


def backend_for_service(db: Session, service: str) -> str:
    return address_for_source(db, service)
