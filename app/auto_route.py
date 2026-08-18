"""Gateway model aliases: auto / auto-quality / auto-long.

Clients send these ids; auth rewrites to real catalog names before
source pick, grants, and llama-router. Vision still uses Auto-VL after this.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .vision_route import rewrite_json_model

if TYPE_CHECKING:
    from .data.models import AuthSettings

# Recommended Halo daily / quality / long-agent targets (Settings defaults).
DEFAULT_AUTO_MODEL = "Qwen3.6-35B-A3B-MTP-UD-Q4_K_XL"
DEFAULT_AUTO_QUALITY = "Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL"
DEFAULT_AUTO_LONG = "Qwen3.6-35B-A3B-MTP-UD-Q5_K_XL"

_ALIASES = {
    "auto": "default",
    "auto-quality": "quality",
    "auto-long": "long",
}

_SLOT_LABEL = {
    "default": "Daily coding / chat (MoE + MTP)",
    "quality": "Higher quality MoE (Q5 MTP)",
    "long": "Higher quality MoE for agents (Q5 MTP, 128k ctx)",
}


def auto_alias_slot(model: str | None) -> str | None:
    """Return policy slot name, or None if this is a real model id."""
    if not model:
        return None
    return _ALIASES.get(model.strip().lower())


def target_for_slot(auth: AuthSettings | None, slot: str) -> str | None:
    """Configured catalog id, or Halo built-in default for that slot."""
    fallback = {
        "default": DEFAULT_AUTO_MODEL,
        "quality": DEFAULT_AUTO_QUALITY,
        "long": DEFAULT_AUTO_LONG,
    }.get(slot)
    if auth is None:
        return fallback
    raw = {
        "default": getattr(auth, "auto_model_default", None),
        "quality": getattr(auth, "auto_model_quality", None),
        "long": getattr(auth, "auto_model_long", None),
    }.get(slot)
    mid = (raw or "").strip()
    return mid or fallback


def resolve_auto_model(auth: AuthSettings | None, model: str | None) -> str | None:
    """Real model id if ``model`` is an auto alias with a configured target."""
    slot = auto_alias_slot(model)
    if slot is None:
        return None
    return target_for_slot(auth, slot)


def rewrite_auto_model(
    body: bytes | None,
    *,
    asked: str | None,
    auth: AuthSettings | None,
) -> tuple[bytes | None, str | None]:
    """Rewrite JSON body when asked id is auto*. Returns (body, resolved_or_None)."""
    target = resolve_auto_model(auth, asked)
    if not target or not body:
        return body, None
    if asked and asked.strip() == target:
        return body, None
    return rewrite_json_model(body, target), target


def auto_alias_list_entries(auth: AuthSettings | None) -> list[dict]:
    """Synthetic /v1/models rows so IDEs can pick auto without catalog sync."""
    if auth is None:
        return []
    out: list[dict] = []
    for alias, slot in (("auto", "default"), ("auto-quality", "quality"), ("auto-long", "long")):
        target = target_for_slot(auth, slot)
        if not target:
            continue
        label = _SLOT_LABEL[slot]
        out.append(
            {
                "id": alias,
                "object": "model",
                "owned_by": "gateway",
                "created": 0,
                "description": f"{label} → {target}",
            }
        )
    return out
