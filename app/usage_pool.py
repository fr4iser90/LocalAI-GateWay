"""Usage pool: prompt-token budget × model weight, windowed reset.

Optional GPU Wh from the source-sidecar (estimate at auth time).
Budget is in tokens (tokens_per_unit is always 1 after migrate).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .data.models import (
    AdminUser,
    ApiKey,
    AuthSettings,
    CatalogModel,
    UsageEvent,
    utcnow,
)
from .privacy import estimate_prompt_tokens

_FALLBACK_TOKENS_PER_SEC = 50.0


@dataclass
class PoolDecision:
    allowed: bool
    reason: str = "ok"
    cost: float = 0.0
    used: float = 0.0
    limit: float = 0.0
    resets_in_sec: int | None = None
    watts: float | None = None
    watt_hours: float | None = None


def observed_tokens_per_sec(db: Session, *, lookback: int = 40) -> float | None:
    """Mean tok/s from recent OK usage events (tokens ÷ wall duration).

    Prompt processing vs generation differ; this is a blunt average when
    enough metered calls exist — never a manual Settings knob.
    """
    if lookback < 1:
        return None
    rows = (
        db.query(UsageEvent)
        .filter(
            UsageEvent.duration_ms.isnot(None),
            UsageEvent.duration_ms > 50,
            UsageEvent.result == "ok",
        )
        .order_by(UsageEvent.id.desc())
        .limit(max(lookback * 4, 40))
        .all()
    )
    rates: list[float] = []
    for r in rows:
        total = int(r.tokens_in or 0) + int(r.tokens_out or 0)
        if total < 1:
            continue
        sec = float(r.duration_ms or 0) / 1000.0
        if sec <= 0:
            continue
        rates.append(total / sec)
        if len(rates) >= lookback:
            break
    if len(rates) < 3:
        return None
    return round(sum(rates) / len(rates), 1)


def resolve_tokens_per_sec(db: Session | None = None) -> float:
    """Observed average when enough data; else a conservative fallback."""
    if db is not None:
        obs = observed_tokens_per_sec(db)
        if obs is not None and obs >= 1.0:
            return obs
    return _FALLBACK_TOKENS_PER_SEC


def migrate_pool_to_token_budget(db: Session, auth: AuthSettings) -> bool:
    """Scale legacy 'units' (tokens÷N) to raw tokens; set tokens_per_unit=1.

    Returns True if a conversion ran.
    """
    tpu = int(getattr(auth, "pool_tokens_per_unit", 1) or 1)
    if tpu <= 1:
        auth.pool_tokens_per_unit = 1
        return False
    for u in db.query(AdminUser).all():
        if u.pool_limit is not None and u.pool_limit > 0:
            u.pool_limit = int(u.pool_limit) * tpu
        used = float(u.pool_used or 0.0)
        if used > 0:
            u.pool_used = used * float(tpu)
    auth.pool_tokens_per_unit = 1
    return True


def _pool_settings(auth: AuthSettings) -> tuple[int, int, float, float, float]:
    window = int(getattr(auth, "pool_window_hours", 5) or 0)
    tpu = int(getattr(auth, "pool_tokens_per_unit", 1) or 1)
    if tpu < 1:
        tpu = 1
    min_cost = float(getattr(auth, "pool_min_cost", 1.0) or 1.0)
    if min_cost < 0:
        min_cost = 0.0
    watt_w = float(getattr(auth, "pool_watt_weight", 0.0) or 0.0)
    if watt_w < 0:
        watt_w = 0.0
    # Legacy column; real estimates use resolve_tokens_per_sec(db).
    tps = float(getattr(auth, "pool_tokens_per_sec", _FALLBACK_TOKENS_PER_SEC) or _FALLBACK_TOKENS_PER_SEC)
    if tps < 1:
        tps = _FALLBACK_TOKENS_PER_SEC
    return window, tpu, min_cost, watt_w, tps


def model_usage_weight(
    db: Session,
    *,
    service: str,
    model: str | None,
    auth: AuthSettings | None = None,
) -> float:
    """Optional per-model budget factor. Always 1.0 unless explicitly enabled.

    Does not alter logged usage tokens — only the optional token-budget charge.
    """
    if auth is None or not bool(getattr(auth, "pool_model_weights_enabled", False)):
        return 1.0
    if not model or not service:
        return 1.0
    row = (
        db.query(CatalogModel)
        .filter(
            CatalogModel.source_name == service,
            CatalogModel.model_id == model,
        )
        .first()
    )
    if row is None:
        return 1.0
    w = float(getattr(row, "usage_weight", 1.0) or 1.0)
    return w if w > 0 else 1.0


def compute_cost(
    *,
    tokens: int | None,
    weight: float,
    tokens_per_unit: int,
    min_cost: float,
    watt_hours: float | None = None,
    watt_weight: float = 0.0,
) -> float:
    """Token-budget points burned by one call (with tokens_per_unit=1 → tokens)."""
    tok = max(0, int(tokens or 0))
    base = max(min_cost, tok / float(tokens_per_unit)) * max(0.0, weight)
    if watt_hours is not None and watt_weight > 0 and watt_hours > 0:
        base += float(watt_hours) * watt_weight
    return round(base, 4)


def fetch_gpu_watts(url: str | None = None) -> float | None:
    """Probe the source-sidecar power endpoint. None if disabled/unreachable."""
    u = (url if url is not None else get_settings().gpu_power_url or "").strip()
    if not u:
        return None
    try:
        with httpx.Client(timeout=0.4) as client:
            resp = client.get(u)
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not isinstance(data, dict) or not data.get("ok"):
            return None
        w = data.get("watts")
        return float(w) if w is not None else None
    except Exception:
        return None


def suggest_gpu_power_url(address: str) -> str:
    """http://HOST:9105/power from source address host:port."""
    addr = (address or "").strip()
    if not addr:
        return ""
    host = addr.split(":")[0].strip()
    if not host:
        return ""
    return f"http://{host}:9105/power"


def probe_url_for_source(src) -> str:
    """Sidecar always on the source host: http://HOST:9105/power from address."""
    if src is None:
        return ""
    return suggest_gpu_power_url(getattr(src, "address", "") or "")


def check_probe(url: str) -> tuple[str, float | None, str]:
    """Return (status, watts, detail) — status: ok|unreachable|off."""
    u = (url or "").strip()
    if not u:
        return "off", None, "no probe URL"
    w = fetch_gpu_watts(u)
    if w is None:
        return "unreachable", None, u
    return "ok", w, u


def estimate_watt_hours(
    *,
    watts: float | None,
    tokens: int | None,
    tokens_per_sec: float,
) -> float | None:
    """Auth-time Wh guess: watts × estimated duration. Not metering-grade."""
    if watts is None or watts <= 0:
        return None
    tok = max(100, int(tokens or 0))  # floor so tiny calls still see some Wh
    seconds = max(1.0, tok / float(tokens_per_sec))
    return round((watts * seconds) / 3600.0, 6)  # W · h = Wh


def watt_hours_from_samples(
    *,
    samples: list[float],
    duration_sec: float,
) -> tuple[float | None, float | None]:
    """(avg_watts, Wh) from real wall time × mean sampled draw."""
    if not samples or duration_sec <= 0:
        return None, None
    avg = sum(samples) / len(samples)
    if avg <= 0:
        return None, None
    wh = round((avg * float(duration_sec)) / 3600.0, 6)
    return round(avg, 2), wh


def _maybe_reset_window(owner: AdminUser, window_hours: int) -> int | None:
    """Reset pool_used if window elapsed. Returns seconds until next reset."""
    if window_hours <= 0:
        return None
    now = utcnow()
    start = owner.pool_window_start
    if start is not None and start.tzinfo is None:
        from datetime import timezone

        start = start.replace(tzinfo=timezone.utc)
    window = timedelta(hours=window_hours)
    if start is None or now >= start + window:
        owner.pool_used = 0.0
        owner.pool_window_start = now
        return int(window.total_seconds())
    remaining = (start + window) - now
    return max(1, int(remaining.total_seconds()))


def sample_power(
    *,
    tokens: int | None,
    tokens_per_sec: float | None = None,
    url: str | None = None,
    db: Session | None = None,
) -> tuple[float | None, float | None]:
    """(watts, watt_hours) from sidecar; (None, None) if off/unreachable."""
    tps = float(tokens_per_sec) if tokens_per_sec and tokens_per_sec >= 1 else resolve_tokens_per_sec(db)
    watts = fetch_gpu_watts(url)
    wh = estimate_watt_hours(watts=watts, tokens=tokens, tokens_per_sec=tps)
    return watts, wh


def check_and_consume_pool(
    db: Session,
    *,
    api_key: ApiKey,
    auth: AuthSettings,
    service: str,
    model: str | None,
    body: bytes | None,
) -> PoolDecision:
    """No-op (allowed) when pool disabled for user/admin or window=0 globally."""
    owner = api_key.owner
    if owner is None or owner.is_platform_admin:
        return PoolDecision(True)
    limit = owner.pool_limit
    if limit is None or limit <= 0:
        return PoolDecision(True)

    window_h, tpu, min_cost, watt_w, _tps_legacy = _pool_settings(auth)
    if window_h <= 0:
        return PoolDecision(True)

    # One-shot: legacy unit budgets → token budgets.
    if migrate_pool_to_token_budget(db, auth):
        tpu = 1
        limit = owner.pool_limit
        if limit is None or limit <= 0:
            return PoolDecision(True)

    resets_in = _maybe_reset_window(owner, window_h)
    tokens = estimate_prompt_tokens(body)
    weight = model_usage_weight(db, service=service, model=model, auth=auth)

    watts = None
    wh = None
    if watt_w > 0:
        watts = fetch_gpu_watts()
        wh = estimate_watt_hours(
            watts=watts,
            tokens=tokens,
            tokens_per_sec=resolve_tokens_per_sec(db),
        )

    cost = compute_cost(
        tokens=tokens,
        weight=weight,
        tokens_per_unit=tpu,
        min_cost=min_cost,
        watt_hours=wh,
        watt_weight=watt_w,
    )
    used = float(owner.pool_used or 0.0)
    if used + cost > float(limit):
        return PoolDecision(
            allowed=False,
            reason="usage_pool_exhausted",
            cost=cost,
            used=used,
            limit=float(limit),
            resets_in_sec=resets_in,
            watts=watts,
            watt_hours=wh,
        )
    owner.pool_used = used + cost
    return PoolDecision(
        allowed=True,
        cost=cost,
        used=owner.pool_used,
        limit=float(limit),
        resets_in_sec=resets_in,
        watts=watts,
        watt_hours=wh,
    )
