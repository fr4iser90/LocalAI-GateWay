"""Platform model catalog: discover from sources, admin enable/disable, filter /v1/models."""

from __future__ import annotations

from dataclasses import dataclass

import httpx
from sqlalchemy.orm import Session, joinedload

from .backends import list_sources
from .db import hash_api_key
from .models import WebUser, ApiKey, CatalogModel, Team, utcnow

_UNRANKED = 10_000

# Suggested capability tags for admin UI (free-form still allowed)
TAG_SUGGESTIONS = (
    "tools",
    "vision",
    "code",
    "fast",
    "medium",
    "slow",
    "long-ctx",
    "reasoning",
    "multilingual",
    "embed",
)


@dataclass
class DiscoveredModel:
    """One upstream model listing entry. Only fields the source actually sent."""

    model_id: str
    upstream_status: str = ""
    ctx_size: int | None = None
    n_ctx: int | None = None
    n_ctx_train: int | None = None
    n_embd: int | None = None
    n_params: int | None = None
    model_size: int | None = None
    modalities_in: str = ""
    modalities_out: str = ""
    has_meta: bool = False


def parse_tags(raw: str | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in (raw or "").replace(";", ",").split(","):
        tag = part.strip().lower().replace(" ", "-")
        if not tag or tag in seen:
            continue
        seen.add(tag)
        out.append(tag)
    return out


def format_tags(tags: list[str] | str | None) -> str:
    if isinstance(tags, str):
        tags = parse_tags(tags)
    return ",".join(parse_tags(",".join(tags or [])))


def suggest_docs_url(model_id: str) -> str:
    """Best-effort HF link when id looks like org/name (never auto-persisted)."""
    mid = (model_id or "").strip().split(":")[0]
    if "/" not in mid or mid.startswith("http") or " " in mid:
        return ""
    org, _, name = mid.partition("/")
    if not org or not name or "/" in name:
        return ""
    if not all(c.isalnum() or c in "-_." for c in org + name):
        return ""
    return f"https://huggingface.co/{org}/{name}"


def infer_speed_tag(model_id: str, kind: str) -> str | None:
    """Vulkan bench tiers (tg128) — Strix Halo; used for gateway routing hints."""
    if kind != "chat":
        return None
    mid = (model_id or "").lower()
    if any(x in mid for x in ("qwen3.8-27b", "qwen3-8b")):
        return "slow"
    if any(x in mid for x in ("qwen3.6-35b", "agentworld-35b")):
        return "medium"
    if any(x in mid for x in ("coder-30b", "qwen3-4b", "nemotron-3-nano-4b", "nemotron-3-nano-30b")):
        return "fast"
    return None


def infer_tags(model_id: str, kind: str) -> list[str]:
    """Heuristic tags from id/kind — providers rarely expose capabilities."""
    mid = (model_id or "").lower()
    tags: list[str] = []
    if kind == "embed" or "embed" in mid or mid.startswith(("bge-", "e5-", "gte-")):
        tags.append("embed")
    if kind == "stt":
        tags.append("stt")
    if kind == "tts":
        tags.append("tts")
    if kind == "chat":
        if mid.endswith("-vl") or "-vl-" in mid or "vision" in mid:
            tags.append("vision")
        if any(x in mid for x in ("code", "coder", "cod-", "gemcod")):
            tags.append("code")
        if any(x in mid for x in ("qwen", "nemotron", "agent", "tool")):
            tags.append("tools")
        speed = infer_speed_tag(model_id, kind)
        if speed:
            tags.append(speed)
        elif any(x in mid for x in ("nano", "270m", "0.5b", "1b", "1.5b", "3b", "4b")):
            tags.append("fast")
        if any(x in mid for x in ("qwen", "mistral", "gemma")):
            tags.append("multilingual")
        if any(x in mid for x in ("reason", "r1", "think", "mtp")):
            tags.append("reasoning")
    return parse_tags(",".join(tags))


def apply_inferred_tags(row: CatalogModel, *, only_if_empty: bool = True) -> None:
    if only_if_empty and (row.tags or "").strip():
        return
    inferred = infer_tags(row.model_id, row.kind)
    if inferred:
        row.tags = format_tags(inferred)


def catalog_grouped_by_kind(rows: list[CatalogModel]) -> list[tuple[str, list[CatalogModel]]]:
    order = ("chat", "embed", "stt", "tts")
    buckets: dict[str, list[CatalogModel]] = {k: [] for k in order}
    other: list[CatalogModel] = []
    for row in rows:
        if row.kind in buckets:
            buckets[row.kind].append(row)
        else:
            other.append(row)
    out: list[tuple[str, list[CatalogModel]]] = [
        (k, buckets[k]) for k in order if buckets[k]
    ]
    if other:
        out.append(("other", other))
    return out


def update_catalog_meta(
    db: Session,
    catalog_id: int,
    *,
    tags: str | None = None,
    short_note: str | None = None,
    docs_url: str | None = None,
    usage_weight: float | None = None,
) -> CatalogModel | None:
    row = db.get(CatalogModel, catalog_id)
    if row is None:
        return None
    if tags is not None:
        row.tags = format_tags(tags)
    if short_note is not None:
        row.short_note = (short_note or "").strip()[:512]
    if docs_url is not None:
        url = (docs_url or "").strip()[:512]
        if url and not (url.startswith("https://") or url.startswith("http://")):
            url = ""
        row.docs_url = url
    if usage_weight is not None:
        row.usage_weight = max(0.01, float(usage_weight))
    return row


def list_catalog(db: Session) -> list[CatalogModel]:
    return (
        db.query(CatalogModel)
        .order_by(CatalogModel.kind, CatalogModel.source_name, CatalogModel.model_id)
        .all()
    )


def get_catalog_entry(db: Session, source_name: str, model_id: str) -> CatalogModel | None:
    return (
        db.query(CatalogModel)
        .filter(
            CatalogModel.source_name == source_name,
            CatalogModel.model_id == model_id,
        )
        .first()
    )


def is_model_globally_enabled(db: Session, source_name: str, model_id: str) -> bool:
    """True only if an enabled catalog row on this source matches the request model."""
    from .backends import model_match_score

    mid = (model_id or "").strip()
    if not mid:
        return False
    rows = (
        db.query(CatalogModel)
        .filter(CatalogModel.source_name == source_name)
        .all()
    )
    best: int | None = None
    enabled_best = False
    for row in rows:
        score = model_match_score(row.model_id, mid)
        if score is None:
            continue
        if best is None or score > best:
            best = score
            enabled_best = bool(row.enabled)
    return bool(best is not None and enabled_best)


def set_model_enabled(db: Session, catalog_id: int, enabled: bool) -> CatalogModel | None:
    row = db.get(CatalogModel, catalog_id)
    if row is None:
        return None
    row.enabled = enabled
    return row


def _as_int(value) -> int | None:
    if value is None or value is False:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _join_modalities(raw) -> str:
    if not isinstance(raw, list):
        return ""
    parts = [str(x).strip() for x in raw if str(x).strip()]
    return ",".join(parts)[:128]


def ctx_size_from_args(args) -> int | None:
    """Extract --ctx-size / -c from llama.cpp status.args when present."""
    if not isinstance(args, list):
        return None
    for i, arg in enumerate(args):
        if arg in ("--ctx-size", "-c") and i + 1 < len(args):
            return _as_int(args[i + 1])
    return None


def parse_openai_model_item(item: dict) -> DiscoveredModel | None:
    """Map one /v1/models data[] entry. Does not invent missing meta fields."""
    if not isinstance(item, dict):
        return None
    mid = item.get("id")
    if not mid:
        return None
    disc = DiscoveredModel(model_id=str(mid))
    status = item.get("status")
    if isinstance(status, dict):
        value = status.get("value")
        if value is not None and str(value).strip():
            disc.upstream_status = str(value).strip()[:32]
        disc.ctx_size = ctx_size_from_args(status.get("args"))
    arch = item.get("architecture")
    if isinstance(arch, dict):
        disc.modalities_in = _join_modalities(arch.get("input_modalities"))
        disc.modalities_out = _join_modalities(arch.get("output_modalities"))
    meta = item.get("meta")
    if isinstance(meta, dict) and meta:
        disc.has_meta = True
        disc.n_ctx = _as_int(meta.get("n_ctx"))
        disc.n_ctx_train = _as_int(meta.get("n_ctx_train"))
        disc.n_embd = _as_int(meta.get("n_embd"))
        disc.n_params = _as_int(meta.get("n_params"))
        disc.model_size = _as_int(meta.get("size"))
    return disc


def apply_discovered_fields(row: CatalogModel, disc: DiscoveredModel, *, now) -> None:
    """Upsert source fields. Meta values are last-known: never cleared on unload."""
    if disc.upstream_status:
        row.upstream_status = disc.upstream_status
    if disc.ctx_size is not None:
        row.ctx_size = disc.ctx_size
    if disc.modalities_in:
        row.modalities_in = disc.modalities_in
    if disc.modalities_out:
        row.modalities_out = disc.modalities_out
    if disc.has_meta:
        if disc.n_ctx is not None:
            row.n_ctx = disc.n_ctx
        if disc.n_ctx_train is not None:
            row.n_ctx_train = disc.n_ctx_train
        if disc.n_embd is not None:
            row.n_embd = disc.n_embd
        if disc.n_params is not None:
            row.n_params = disc.n_params
        if disc.model_size is not None:
            row.model_size = disc.model_size
        row.upstream_meta_at = now


def format_param_count(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + "B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(n)


def format_bytes(n: int | None) -> str:
    if n is None:
        return ""
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f}".rstrip("0").rstrip(".") + " GiB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.0f} MiB"
    return f"{n} B"


def _fetch_openai_models(base: str) -> list[DiscoveredModel]:
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        resp = client.get(f"{base}/v1/models")
        if resp.status_code != 200:
            return []
        data = resp.json()
        out: list[DiscoveredModel] = []
        for item in data.get("data") or []:
            disc = parse_openai_model_item(item) if isinstance(item, dict) else None
            if disc:
                out.append(disc)
        return out


def _fetch_ollama_tags(base: str) -> list[DiscoveredModel]:
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        resp = client.get(f"{base}/api/tags")
        if resp.status_code != 200:
            return []
        data = resp.json()
        out: list[DiscoveredModel] = []
        for item in data.get("models") or []:
            name = item.get("name") or item.get("model")
            if name:
                out.append(DiscoveredModel(model_id=str(name)))
        return out


def _fetch_piper_voices(base: str) -> list[DiscoveredModel]:
    """Piper-style root JSON: {\"ok\": true, \"voices\": [\"de_DE-…\"]}."""
    with httpx.Client(timeout=8.0, follow_redirects=True) as client:
        resp = client.get(f"{base}/")
        if resp.status_code != 200:
            return []
        try:
            data = resp.json()
        except Exception:
            return []
        if not isinstance(data, dict):
            return []
        voices = data.get("voices") or []
        out: list[DiscoveredModel] = []
        for v in voices:
            if isinstance(v, str) and v.strip():
                out.append(DiscoveredModel(model_id=v.strip()))
            elif isinstance(v, dict) and v.get("id"):
                out.append(DiscoveredModel(model_id=str(v["id"])))
            elif isinstance(v, dict) and v.get("name"):
                out.append(DiscoveredModel(model_id=str(v["name"])))
        return out


def discover_models_for_source(address: str, kind: str) -> list[DiscoveredModel]:
    if not address:
        return []
    base = f"http://{address}"
    models = _fetch_openai_models(base)
    if models:
        return models
    if kind == "chat":
        return _fetch_ollama_tags(base)
    if kind == "tts":
        return _fetch_piper_voices(base)
    # stt / whisper.cpp: usually one model at process start, no list API
    return []


def sync_catalog_from_sources(db: Session) -> dict[str, int]:
    """Upsert models from sources. Keeps enabled + admin metadata; fills empty tags.

    Upstream numeric meta is last-known: updated when the source sends ``meta``,
    retained when the model is listed unloaded without ``meta``.
    """
    seen = 0
    created = 0
    tagged = 0
    meta_updates = 0
    now = utcnow()
    for src in list_sources(db):
        if not (src.address or "").strip():
            continue
        if src.kind not in ("chat", "embed", "stt", "tts"):
            continue
        try:
            discovered = discover_models_for_source(src.address.strip(), src.kind)
        except Exception:
            discovered = []
        if not discovered and src.kind in ("stt", "tts"):
            # Placeholder so the source appears in catalog (whisper: no model list)
            discovered = [DiscoveredModel(model_id=src.name)]
        for disc in discovered:
            seen += 1
            row = get_catalog_entry(db, src.name, disc.model_id)
            if row is None:
                row = CatalogModel(
                    source_name=src.name,
                    kind=src.kind,
                    model_id=disc.model_id,
                    enabled=True,
                    last_seen_at=now,
                )
                if (
                    src.kind == "stt"
                    and disc.model_id == src.name
                    and not (row.short_note or "").strip()
                ):
                    row.short_note = (
                        "whisper.cpp loads one model at server start; no /v1/models list"
                    )
                apply_discovered_fields(row, disc, now=now)
                apply_inferred_tags(row, only_if_empty=True)
                db.add(row)
                created += 1
                if row.tags:
                    tagged += 1
                if disc.has_meta:
                    meta_updates += 1
            else:
                row.kind = src.kind
                row.last_seen_at = now
                apply_discovered_fields(row, disc, now=now)
                if disc.has_meta:
                    meta_updates += 1
                before = row.tags or ""
                apply_inferred_tags(row, only_if_empty=True)
                if (row.tags or "") != before and row.tags:
                    tagged += 1
    db.flush()
    sources_n = len(
        [
            s
            for s in list_sources(db)
            if s.kind in ("chat", "embed", "stt", "tts") and (s.address or "").strip()
        ]
    )
    return {
        "sources": sources_n,
        "seen": seen,
        "created": created,
        "tagged": tagged,
        "meta": meta_updates,
    }


def models_visible_for_key(db: Session, api_key: ApiKey) -> list[CatalogModel]:
    """Catalog rows the key may see in GET /v1/models (chat/embed only)."""
    from ..auth.check import _models_for_key, _services_for_key

    allowed_sources = _services_for_key(api_key, db)
    rows = list_catalog(db)
    out: list[CatalogModel] = []
    for row in rows:
        if row.kind not in ("chat", "embed"):
            continue
        if not row.enabled:
            continue
        if row.source_name not in allowed_sources:
            continue
        allow = _models_for_key(api_key, row.source_name, db)
        if allow is not None and row.model_id not in allow:
            # also allow bare name match for name:tag
            if not any(
                row.model_id == a or row.model_id.startswith(a + ":") for a in allow
            ):
                continue
        out.append(row)
    return out


def context_length_for_model(row: CatalogModel) -> int | None:
    """OpenRouter-style max context for IDEs (Continue, OpenCode, …).

    Prefer ctx_size (--ctx-size on the loaded slot); else last-known n_ctx_train.
    """
    if row.ctx_size is not None and row.ctx_size > 0:
        return row.ctx_size
    if row.n_ctx_train is not None and row.n_ctx_train > 0:
        return row.n_ctx_train
    return None


def openai_models_payload(rows: list[CatalogModel]) -> dict:
    """OpenAI-compatible list sorted by catalog order."""

    data = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda r: (r.sort_order, r.model_id, r.source_name)):
        if row.model_id in seen:
            continue
        seen.add(row.model_id)
        entry: dict = {
            "id": row.model_id,
            "object": "model",
            "owned_by": row.source_name,
            "created": int((row.created_at or utcnow()).timestamp()),
        }
        if (row.short_note or "").strip():
            entry["description"] = row.short_note
        if row.ctx_size is not None:
            entry["ctx_size"] = row.ctx_size
        ctx_len = context_length_for_model(row)
        if ctx_len is not None:
            entry["context_length"] = ctx_len
        if row.n_ctx is not None:
            entry["n_ctx"] = row.n_ctx
        if row.n_ctx_train is not None:
            entry["n_ctx_train"] = row.n_ctx_train
        if row.n_embd is not None:
            entry["n_embd"] = row.n_embd
        if row.n_params is not None:
            entry["n_params"] = row.n_params
        if row.model_size is not None:
            entry["size"] = row.model_size
        if row.upstream_status:
            entry["status"] = row.upstream_status
        data.append(entry)
    return {"object": "list", "data": data}


def load_api_key(db: Session, raw_key: str | None) -> ApiKey | None:
    if not raw_key:
        return None
    return (
        db.query(ApiKey)
        .options(
            joinedload(ApiKey.team).joinedload(Team.service_grants),
            joinedload(ApiKey.team).joinedload(Team.model_allowlists),
            joinedload(ApiKey.owner).joinedload(WebUser.service_grants),
            joinedload(ApiKey.owner).joinedload(WebUser.model_allowlists),
            joinedload(ApiKey.service_grants),
            joinedload(ApiKey.model_allowlists),
        )
        .filter(ApiKey.key_hash == hash_api_key(raw_key), ApiKey.is_active.is_(True))
        .first()
    )
