"""Resolve model → source with grants + optional load awareness."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .data.backends import resolve_source_for_kind
from .data.catalog import load_api_key
from .data.grants import effective_services


def allowed_services_for_key(db: Session, raw_key: str | None) -> set[str] | None:
    """None = no grant filter (admin or unknown key)."""
    if not (raw_key or "").strip():
        return None
    api_key = load_api_key(db, raw_key)
    if api_key is None:
        return None
    owner = api_key.owner
    if owner is not None and owner.is_platform_admin:
        return None
    return effective_services(db, api_key)


def resolve_routed_source(
    db: Session,
    kind: str,
    *,
    model: str | None,
    raw_key: str | None,
    load_aware: bool = True,
) -> BackendSource | None:
    allowed = allowed_services_for_key(db, raw_key)
    return resolve_source_for_kind(
        db,
        kind,
        model=model,
        allowed_services=allowed,
        load_aware=load_aware,
    )
