"""Parse OpenAI usage + llama.cpp timings from upstream chat responses."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass
class UpstreamMetrics:
    tokens_in: int | None = None
    tokens_out: int | None = None
    pp_tok_s: float | None = None
    tg_tok_s: float | None = None


def _as_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _as_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def metrics_from_payload(
    data: dict[str, Any],
    *,
    duration_ms: float | None = None,
) -> UpstreamMetrics:
    """Extract tokens + PP/TG from a chat.completion (or stream final) object."""
    out = UpstreamMetrics()
    usage = data.get("usage")
    if isinstance(usage, dict):
        out.tokens_in = _as_int(usage.get("prompt_tokens"))
        out.tokens_out = _as_int(usage.get("completion_tokens"))

    timings = data.get("timings")
    if isinstance(timings, dict):
        out.pp_tok_s = _as_float(timings.get("prompt_per_second"))
        out.tg_tok_s = _as_float(timings.get("predicted_per_second"))

    # Fallback TG: completion tokens / wall seconds (blended if no timings).
    if out.tg_tok_s is None and out.tokens_out and duration_ms and duration_ms > 50:
        sec = float(duration_ms) / 1000.0
        if sec > 0:
            out.tg_tok_s = round(out.tokens_out / sec, 3)
    return out


def _try_json_object(raw: bytes | str) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="ignore").strip()
    else:
        text = raw.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _metrics_from_sse(
    raw: bytes,
    *,
    duration_ms: float | None = None,
) -> UpstreamMetrics:
    """Scan SSE ``data:`` lines from the end for usage/timings."""
    text = raw.decode("utf-8", errors="ignore")
    best = UpstreamMetrics()
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        obj = _try_json_object(payload)
        if obj is None:
            continue
        m = metrics_from_payload(obj, duration_ms=duration_ms)
        if m.tokens_in is not None or m.tokens_out is not None:
            if best.tokens_in is None:
                best.tokens_in = m.tokens_in
            if best.tokens_out is None:
                best.tokens_out = m.tokens_out
        if m.pp_tok_s is not None and best.pp_tok_s is None:
            best.pp_tok_s = m.pp_tok_s
        if m.tg_tok_s is not None and best.tg_tok_s is None:
            best.tg_tok_s = m.tg_tok_s
        if (
            best.tokens_out is not None
            and (best.pp_tok_s is not None or best.tg_tok_s is not None)
        ):
            break
    # Re-apply wall fallback if SSE only had usage.
    if best.tg_tok_s is None and best.tokens_out and duration_ms and duration_ms > 50:
        sec = float(duration_ms) / 1000.0
        if sec > 0:
            best.tg_tok_s = round(best.tokens_out / sec, 3)
    return best


def parse_upstream_metrics(
    raw: bytes,
    *,
    duration_ms: float | None = None,
    content_type: str | None = None,
) -> UpstreamMetrics:
    """Parse non-stream JSON or SSE chat response bytes."""
    if not raw:
        return UpstreamMetrics()
    ct = (content_type or "").lower()
    # Prefer SSE when content-type says so, or body looks like SSE.
    looks_sse = "text/event-stream" in ct or b"\ndata:" in raw[:4096] or raw.lstrip().startswith(
        b"data:"
    )
    if looks_sse:
        return _metrics_from_sse(raw, duration_ms=duration_ms)

    obj = _try_json_object(raw)
    if obj is not None:
        return metrics_from_payload(obj, duration_ms=duration_ms)

    # Truncated buffer or mixed: try SSE scan as last resort.
    return _metrics_from_sse(raw, duration_ms=duration_ms)
