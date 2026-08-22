"""Resolve model → source with grants + optional load awareness."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .data.backends import BackendSource, resolve_source_for_kind
from .data.catalog import load_api_key
from .data.grants import effective_services
from .data.routing_strategy import effective_routing_for_key


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
    auth=None,
    routing_strategy: str | None = None,
    preferred_source: str | None = None,
    load_aware: bool | None = None,
) -> BackendSource | None:
    allowed = allowed_services_for_key(db, raw_key)
    api_key = load_api_key(db, raw_key) if raw_key else None

    if routing_strategy is None or preferred_source is None:
        eff_strategy, eff_pref = effective_routing_for_key(auth, api_key)
        if routing_strategy is None:
            routing_strategy = eff_strategy
        if preferred_source is None:
            preferred_source = eff_pref

    return resolve_source_for_kind(
        db,
        kind,
        model=model,
        allowed_services=allowed,
        routing_strategy=routing_strategy or "load_aware",
        preferred_source=preferred_source,
        load_aware=load_aware,
    )
