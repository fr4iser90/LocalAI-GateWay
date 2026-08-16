from __future__ import annotations

import threading
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .crypto_util import hash_audit_chain
from .data.models import AlertConfig, AuditLog, UsageDaily, UsageEvent, utcnow
from .auth.rate_limit import rate_limiter

_alert_lock = threading.Lock()
_last_alert: dict[str, float] = {}

GENESIS = "0" * 64


def _audit_ts(dt: datetime | None) -> str:
    """Canonical UTC timestamp for hash payloads (stable across SQLite round-trips)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}Z"


def _ts_candidates(dt: datetime | None) -> list[str]:
    """Current + legacy isoformats so older rows still verify after the fix."""
    if dt is None:
        return [""]
    out: list[str] = []
    seen: set[str] = set()

    def add(s: str) -> None:
        if s not in seen:
            seen.add(s)
            out.append(s)

    add(_audit_ts(dt))
    add(dt.isoformat())
    if dt.tzinfo is not None:
        add(dt.astimezone(timezone.utc).isoformat())
        add(dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))
        naive = dt.astimezone(timezone.utc).replace(tzinfo=None)
        add(naive.isoformat())
        add(naive.isoformat(" "))
    else:
        add(dt.replace(tzinfo=timezone.utc).isoformat())
        add(dt.isoformat(" "))
        add(dt.strftime("%Y-%m-%d %H:%M:%S.%f"))
    return out


def _audit_payload(
    *,
    created_ts: str,
    actor_user_id: int | None,
    actor_username: str,
    action: str,
    entity_type: str,
    entity_id: str,
    detail: str,
) -> str:
    return "|".join(
        [
            created_ts,
            str(actor_user_id or ""),
            actor_username or "",
            action or "",
            entity_type or "",
            entity_id or "",
            detail or "",
        ]
    )


def write_audit(
    db: Session,
    *,
    actor: object | None,
    action: str,
    entity_type: str = "",
    entity_id: str | int | None = "",
    detail: str = "",
) -> None:
    username = ""
    uid = None
    if actor is not None:
        uid = getattr(actor, "id", None)
        username = getattr(actor, "username", "") or ""
    created = utcnow()
    detail_s = detail[:2000]
    entity_s = str(entity_id or "")

    last = db.query(AuditLog).order_by(AuditLog.id.desc()).first()
    prev = last.entry_hash if last and last.entry_hash else GENESIS
    payload = _audit_payload(
        created_ts=_audit_ts(created),
        actor_user_id=uid,
        actor_username=username,
        action=action,
        entity_type=entity_type,
        entity_id=entity_s,
        detail=detail_s,
    )
    entry = hash_audit_chain(prev, payload)

    db.add(
        AuditLog(
            actor_user_id=uid,
            actor_username=username,
            action=action,
            entity_type=entity_type,
            entity_id=entity_s,
            detail=detail_s,
            created_at=created,
            prev_hash=prev,
            entry_hash=entry,
        )
    )
    db.flush()  # next write_audit in same transaction must see this hash


def verify_audit_chain(db: Session, *, limit: int = 5000) -> tuple[bool, str]:
    """Return (ok, message). Checks hash chain integrity."""
    rows = db.query(AuditLog).order_by(AuditLog.id.asc()).limit(limit).all()
    prev = GENESIS
    for row in rows:
        if row.prev_hash and row.prev_hash != prev and row.entry_hash:
            # allow empty hashes for pre-migration rows
            if row.prev_hash != "" and row.entry_hash != "":
                return False, f"chain break at audit id={row.id}"
        if row.entry_hash:
            matched = False
            for ts in _ts_candidates(row.created_at):
                payload = _audit_payload(
                    created_ts=ts,
                    actor_user_id=row.actor_user_id,
                    actor_username=row.actor_username or "",
                    action=row.action or "",
                    entity_type=row.entity_type or "",
                    entity_id=row.entity_id or "",
                    detail=row.detail or "",
                )
                expected = hash_audit_chain(row.prev_hash or GENESIS, payload)
                if expected == row.entry_hash:
                    matched = True
                    break
            if row.prev_hash and not matched:
                return False, f"hash mismatch at audit id={row.id}"
            prev = row.entry_hash
        else:
            prev = GENESIS
    return True, f"ok ({len(rows)} rows checked)"


def bump_usage_daily(
    db: Session,
    *,
    team_id: int | None,
    api_key_id: int | None,
    team_name: str,
    key_label: str,
    service: str,
    model: str | None,
    result: str,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    audio_seconds: float | None = None,
    response_chars: int | None = None,
    duration_ms: float | None = None,
    day=None,
    watt_hours: float | None = None,
    pool_cost: float | None = None,
) -> None:
    day = day or utcnow().date()
    model_key = model or ""
    row = (
        db.query(UsageDaily)
        .filter(
            UsageDaily.day == day,
            UsageDaily.team_id == team_id,
            UsageDaily.api_key_id == api_key_id,
            UsageDaily.service == service,
            UsageDaily.model == model_key,
        )
        .first()
    )
    if row is None:
        row = UsageDaily(
            day=day,
            team_id=team_id,
            api_key_id=api_key_id,
            team_name=team_name,
            key_label=key_label,
            service=service,
            model=model_key,
        )
        db.add(row)
        db.flush()
    if result == "ok":
        row.ok_count += 1
        row.tokens_in = (row.tokens_in or 0) + (tokens_in or 0)
        row.tokens_out = (row.tokens_out or 0) + (tokens_out or 0)
        row.audio_seconds = float(row.audio_seconds or 0) + float(audio_seconds or 0)
        row.response_chars = (row.response_chars or 0) + (response_chars or 0)
        row.watt_hours = float(row.watt_hours or 0) + float(watt_hours or 0)
        row.pool_cost = float(row.pool_cost or 0) + float(pool_cost or 0)
        if duration_ms is not None:
            row.latency_sum_ms = float(row.latency_sum_ms or 0) + float(duration_ms)
            row.latency_count = (row.latency_count or 0) + 1
    elif result == "rate_limit":
        row.rate_limit_count += 1
    else:
        row.deny_count += 1


def finalize_usage_metering(
    db: Session,
    usage_id: int,
    *,
    duration_ms: float,
    watts: float | None,
    watt_hours: float | None,
    upstream_status: int | None = None,
    power_status: str = "",
) -> None:
    """Patch UsageEvent after upstream finishes (real duration + sampled Wh)."""
    ev = db.get(UsageEvent, usage_id)
    if ev is None:
        return
    prev_wh = float(ev.watt_hours or 0)
    prev_dur = ev.duration_ms
    ev.duration_ms = float(duration_ms)
    ev.watts = watts
    ev.watt_hours = watt_hours
    if power_status:
        ev.power_status = power_status[:32]
    elif watts is not None:
        ev.power_status = "metered"
    if upstream_status is not None:
        ev.status = int(upstream_status)

    day = utcnow().date()
    if ev.created_at is not None:
        created = ev.created_at
        if created.tzinfo is None:
            from datetime import timezone

            created = created.replace(tzinfo=timezone.utc)
        day = created.date()

    row = (
        db.query(UsageDaily)
        .filter(
            UsageDaily.day == day,
            UsageDaily.team_id == ev.team_id,
            UsageDaily.api_key_id == ev.api_key_id,
            UsageDaily.service == ev.service,
            UsageDaily.model == (ev.model or ""),
        )
        .first()
    )
    if row is not None:
        row.watt_hours = max(0.0, float(row.watt_hours or 0) - prev_wh + float(watt_hours or 0))
        if prev_dur is None:
            row.latency_sum_ms = float(row.latency_sum_ms or 0) + float(duration_ms)
            row.latency_count = (row.latency_count or 0) + 1
        else:
            row.latency_sum_ms = (
                float(row.latency_sum_ms or 0) - float(prev_dur) + float(duration_ms)
            )
    db.commit()


def _should_fire(key: str, cooldown_s: float = 300.0) -> bool:
    import time

    now = time.time()
    with _alert_lock:
        last = _last_alert.get(key, 0.0)
        if now - last < cooldown_s:
            return False
        _last_alert[key] = now
        return True


def maybe_alert(
    db: Session,
    *,
    event: str,
    message: str,
    quota_pct: float | None = None,
) -> None:
    cfg = db.query(AlertConfig).first()
    if cfg is None or not cfg.enabled or not cfg.webhook_url.strip():
        return
    if event == "quota" and not cfg.alert_on_quota:
        return
    if event == "rate_limit" and not cfg.alert_on_rate_limit:
        return
    if event == "quota" and quota_pct is not None and quota_pct < cfg.quota_warn_pct:
        return
    if not _should_fire(f"{event}:{message[:80]}"):
        return
    payload = {"text": f"[LocalAI-Gateway] {event}: {message}", "event": event, "message": message}
    try:
        httpx.post(cfg.webhook_url.strip(), json=payload, timeout=3.0)
    except Exception:
        pass


def check_quota_alert(
    db: Session,
    *,
    key_id: int,
    team_id: int | None,
    key_q: int | None,
    team_q: int | None,
    label: str,
    user_id: int | None = None,
    user_q: int | None = None,
) -> None:
    pct = rate_limiter.quota_usage_pct(
        key_id=key_id,
        team_id=team_id,
        key_q=key_q,
        team_q=team_q,
        user_id=user_id,
        user_q=user_q,
    )
    if pct is None:
        return
    maybe_alert(
        db,
        event="quota",
        message=f"key={label} usage={pct:.0f}% of daily quota",
        quota_pct=pct,
    )
