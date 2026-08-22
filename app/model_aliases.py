"""Public model aliases: stable client ids → real catalog model + optional source pin."""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy.orm import Session

from .data.models import CatalogModel, ModelAlias, utcnow
from .vision_route import rewrite_json_model

_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,126}$")


@dataclass(frozen=True)
class AliasResolution:
    alias_id: str
    target_model_id: str
    preferred_source: str | None = None


def normalize_alias_id(raw: str | None) -> str:
    return (raw or "").strip().lower()


def validate_alias_id(raw: str | None) -> str | None:
    """Return normalized id or None if invalid."""
    mid = normalize_alias_id(raw)
    if not mid or not _ALIAS_RE.match(mid):
        return None
    # Reserved auto slots stay on auto_route.py
    if mid in {"auto", "auto-quality", "auto-long"}:
        return None
    return mid


def get_alias(db: Session, alias_id: str | None) -> ModelAlias | None:
    mid = normalize_alias_id(alias_id)
    if not mid:
        return None
    return (
        db.query(ModelAlias)
        .filter(ModelAlias.alias_id == mid, ModelAlias.enabled.is_(True))
        .first()
    )


def resolve_alias(db: Session, model: str | None) -> AliasResolution | None:
    row = get_alias(db, model)
    if row is None:
        return None
    target = (row.target_model_id or "").strip()
    if not target:
        return None
    pref = (row.preferred_source or "").strip().lower() or None
    return AliasResolution(
        alias_id=row.alias_id,
        target_model_id=target,
        preferred_source=pref,
    )


def rewrite_model_alias(
    db: Session,
    body: bytes | None,
    *,
    asked: str | None,
) -> tuple[bytes | None, AliasResolution | None]:
    """Rewrite JSON body when asked id is a configured alias."""
    resolved = resolve_alias(db, asked)
    if resolved is None or not body:
        return body, None
    if asked and asked.strip() == resolved.target_model_id:
        return body, resolved
    return rewrite_json_model(body, resolved.target_model_id), resolved


def apply_client_model_rewrites(
    db: Session,
    body: bytes | None,
    *,
    asked: str | None,
    auth,
) -> tuple[bytes | None, str | None, str | None]:
    """Apply auto-* then custom aliases. Returns (body, rewritten_id, preferred_source)."""
    from .auto_route import rewrite_auto_model

    body, auto_to = rewrite_auto_model(body, asked=asked, auth=auth)
    next_ask = auto_to or asked
    body, alias = rewrite_model_alias(db, body, asked=next_ask)
    rewritten = (alias.target_model_id if alias else None) or auto_to
    pref = alias.preferred_source if alias else None
    return body, rewritten, pref


def list_aliases(db: Session, *, enabled_only: bool = False) -> list[ModelAlias]:
    q = db.query(ModelAlias).order_by(ModelAlias.sort_order, ModelAlias.alias_id)
    if enabled_only:
        q = q.filter(ModelAlias.enabled.is_(True))
    return q.all()


def upsert_alias(
    db: Session,
    *,
    alias_id: str,
    target_model_id: str,
    preferred_source: str = "",
    description: str = "",
    show_backend: bool = True,
    enabled: bool = True,
    kind: str = "chat",
    sort_order: int = 0,
) -> ModelAlias | None:
    mid = validate_alias_id(alias_id)
    target = (target_model_id or "").strip()
    if not mid or not target:
        return None
    row = db.query(ModelAlias).filter(ModelAlias.alias_id == mid).first()
    if row is None:
        row = ModelAlias(alias_id=mid, created_at=utcnow())
        db.add(row)
    row.target_model_id = target[:256]
    row.preferred_source = (preferred_source or "").strip().lower()[:64]
    row.description = (description or "").strip()[:512]
    row.show_backend = bool(show_backend)
    row.enabled = bool(enabled)
    row.kind = (kind or "chat").strip().lower()[:16] or "chat"
    row.sort_order = int(sort_order)
    return row


def delete_alias(db: Session, alias_id: str) -> bool:
    mid = normalize_alias_id(alias_id)
    row = db.query(ModelAlias).filter(ModelAlias.alias_id == mid).first()
    if row is None:
        return False
    db.delete(row)
    return True


def _catalog_row_for_target(db: Session, target: str) -> CatalogModel | None:
    return (
        db.query(CatalogModel)
        .filter(CatalogModel.model_id == target, CatalogModel.enabled.is_(True))
        .order_by(CatalogModel.source_name)
        .first()
    )


def alias_list_entries(db: Session) -> list[dict]:
    """Synthetic /v1/models rows for enabled aliases (clients pick stable ids)."""
    out: list[dict] = []
    for row in list_aliases(db, enabled_only=True):
        target = (row.target_model_id or "").strip()
        if not target:
            continue
        cat = _catalog_row_for_target(db, target)
        desc = (row.description or "").strip()
        if row.show_backend:
            backend_bit = f"→ {target}"
            desc = f"{desc} {backend_bit}".strip() if desc else backend_bit
        entry: dict = {
            "id": row.alias_id,
            "object": "model",
            "owned_by": "onprem",
            "created": int((row.created_at or utcnow()).timestamp()),
            "description": desc or row.alias_id,
        }
        if cat is not None:
            from .data.catalog import (
                architecture_for_openai_payload,
                context_length_for_model,
                tags_for_openai_payload,
            )

            ctx = context_length_for_model(cat)
            if ctx is not None:
                entry["context_length"] = ctx
            if cat.ctx_size is not None:
                entry["ctx_size"] = cat.ctx_size
            tags = tags_for_openai_payload(cat)
            if tags:
                entry["tags"] = tags
            arch = architecture_for_openai_payload(cat)
            if arch:
                entry["architecture"] = arch
        out.append(entry)
    return out
