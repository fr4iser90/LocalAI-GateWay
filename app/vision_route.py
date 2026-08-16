"""Optional vision (VL) model rewrite when chat requests include images.

Off by default (AuthSettings.auto_vl_routing). Detection is best-effort from
OpenAI-style content parts; sibling pick uses catalog tags / id heuristics.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

from sqlalchemy.orm import Session

from .data.catalog import parse_tags
from .data.models import CatalogModel

_VISION_PART_TYPES = frozenset(
    {
        "image_url",
        "image",
        "input_image",
        "image_file",
    }
)
_TICKET_TTL_SEC = 90
_VL_INFIX_RE = re.compile(
    r"(^|[-_.])vl(?=$|[-_.])|vision",
    re.IGNORECASE,
)


def _parse_json_body(body: bytes | None, content_type: str | None) -> dict | None:
    if not body:
        return None
    ct = (content_type or "").lower()
    if "multipart/form-data" in ct:
        return None
    if "application/json" not in ct and body[:1] not in (b"{", b"["):
        return None
    try:
        payload = json.loads(body[:2_000_000].decode("utf-8", errors="ignore"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _part_is_vision(part: Any) -> bool:
    if not isinstance(part, dict):
        return False
    t = str(part.get("type") or "").strip().lower()
    if t in _VISION_PART_TYPES:
        return True
    if "image_url" in part or "image" in part:
        return True
    return False


def request_needs_vision(body: bytes | None, content_type: str | None) -> bool:
    """True when chat JSON clearly carries an image/vision content part."""
    payload = _parse_json_body(body, content_type)
    if not payload:
        return False
    for msg in payload.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            if any(_part_is_vision(p) for p in content):
                return True
        elif isinstance(content, dict) and _part_is_vision(content):
            return True
    # Rare OpenAI-style top-level / Responses API shapes
    for key in ("input", "content"):
        block = payload.get(key)
        if isinstance(block, list) and any(_part_is_vision(p) for p in block):
            return True
    return False


def _row_is_vision(row: CatalogModel) -> bool:
    tags = set(parse_tags(row.tags))
    if "vision" in tags or "vl" in tags:
        return True
    mods = (row.modalities_in or "").lower()
    if "image" in mods or "vision" in mods:
        return True
    mid = (row.model_id or "").lower()
    return bool(_VL_INFIX_RE.search(mid))


def model_is_vision(db: Session, source_name: str, model: str) -> bool:
    if not model:
        return False
    row = (
        db.query(CatalogModel)
        .filter(
            CatalogModel.source_name == source_name,
            CatalogModel.model_id == model,
            CatalogModel.enabled.is_(True),
        )
        .first()
    )
    if row is not None:
        return _row_is_vision(row)
    # Fall back to id heuristic when not in catalog yet
    return bool(_VL_INFIX_RE.search(model.lower()))


def _stem_candidates(model: str) -> list[str]:
    """Generate likely VL sibling ids from a non-VL model id."""
    mid = (model or "").strip()
    if not mid:
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(x: str) -> None:
        x = x.strip()
        if x and x not in seen and x != mid:
            seen.add(x)
            out.append(x)

    # Insert -VL / -vl before common quant / size suffixes
    for sep, token in (("-", "VL"), ("-", "vl"), ("_", "VL"), (".", "VL")):
        add(f"{mid}{sep}{token}")
        m = re.match(
            r"^(.*?)([-_.](?:Q\d[\w.+]*|GGUF|gguf|Instruct|instruct|Chat|chat).*)$",
            mid,
        )
        if m:
            add(f"{m.group(1)}{sep}{token}{m.group(2)}")
        # Qwen-style: Base-7B-Instruct → Base-VL-7B-Instruct
        m2 = re.match(r"^(.*?)([-_.]\d+[Bb].*)$", mid)
        if m2:
            add(f"{m2.group(1)}{sep}{token}{m2.group(2)}")

    # Replace trailing Instruct with VL-Instruct patterns already covered
    return out


def _match_vl_among(model_id: str, vision_rows: list[CatalogModel]) -> CatalogModel | None:
    if not model_id or not vision_rows:
        return None
    by_id = {r.model_id: r for r in vision_rows}
    for cand in _stem_candidates(model_id):
        if cand in by_id:
            return by_id[cand]
        for mid, row in by_id.items():
            if mid.lower() == cand.lower():
                return row
    ml = model_id.lower()
    best: tuple[int, CatalogModel] | None = None
    for row in vision_rows:
        ol = row.model_id.lower()
        compact_m = _VL_INFIX_RE.sub("", ml)
        compact_o = _VL_INFIX_RE.sub("", ol)
        if compact_m and compact_o and (
            compact_m == compact_o
            or compact_m in compact_o
            or compact_o in compact_m
        ):
            score = min(len(compact_m), len(compact_o))
            if best is None or score > best[0]:
                best = (score, row)
    if best and best[0] >= max(6, len(ml) // 3):
        return best[1]
    return None


def group_models_vl_pairs(
    models: list[CatalogModel],
) -> list[tuple[CatalogModel, CatalogModel | None]]:
    """Order-preserving: (base, vl) pairs or (solo, None).

    Used by the admin picker when auto-VL routing is on so text+VL
    siblings show as one coupled row instead of two independent lines.
    """
    if not models:
        return []
    vision = [m for m in models if _row_is_vision(m)]
    pairs: dict[str, CatalogModel] = {}
    used_vl: set[str] = set()
    for m in models:
        if _row_is_vision(m):
            continue
        sib = _match_vl_among(m.model_id, vision)
        if sib is None or sib.model_id in used_vl:
            continue
        pairs[m.model_id] = sib
        used_vl.add(sib.model_id)

    out: list[tuple[CatalogModel, CatalogModel | None]] = []
    seen: set[str] = set()
    for m in models:
        if m.model_id in seen:
            continue
        if m.model_id in pairs:
            vl = pairs[m.model_id]
            out.append((m, vl))
            seen.add(m.model_id)
            seen.add(vl.model_id)
        elif m.model_id in used_vl:
            continue
        else:
            out.append((m, None))
            seen.add(m.model_id)
    return out


def resolve_vl_model(db: Session, source_name: str, model: str | None) -> str | None:
    """Pick an enabled vision sibling on the same source, or None."""
    if not model or not source_name:
        return None
    if model_is_vision(db, source_name, model):
        return None

    rows = (
        db.query(CatalogModel)
        .filter(
            CatalogModel.source_name == source_name,
            CatalogModel.enabled.is_(True),
            CatalogModel.kind == "chat",
        )
        .all()
    )
    vision_rows = [r for r in rows if _row_is_vision(r)]
    sib = _match_vl_among(model, vision_rows)
    return sib.model_id if sib else None


def rewrite_json_model(body: bytes, new_model: str) -> bytes:
    """Replace top-level JSON \"model\" field; leave body unchanged on failure."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except Exception:
        return body
    if not isinstance(payload, dict):
        return body
    payload["model"] = new_model
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def mint_forward_ticket(
    *,
    secret: str,
    service: str,
    backend: str,
    rewrite_uri: str,
    rewrite_model: str = "",
    usage_id: int | None = None,
) -> str:
    """Ticket for nginx → /v1/gateway/forward (VL rewrite and/or usage metering)."""
    ts = int(time.time())
    payload: dict = {
        "ts": ts,
        "service": service,
        "backend": backend,
        "uri": rewrite_uri,
        "model": rewrite_model or "",
    }
    if usage_id is not None:
        payload["uid"] = int(usage_id)
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(raw).decode("ascii") + "." + sig


def parse_forward_ticket(ticket: str, secret: str) -> dict | None:
    if not ticket or "." not in ticket:
        return None
    b64, _, sig = ticket.partition(".")
    try:
        raw = base64.urlsafe_b64decode(b64.encode("ascii"))
    except Exception:
        return None
    expect = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expect, sig):
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    ts = int(payload.get("ts") or 0)
    if abs(int(time.time()) - ts) > _TICKET_TTL_SEC:
        return None
    return payload
