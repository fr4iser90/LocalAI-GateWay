from __future__ import annotations

import threading

import httpx
from sqlalchemy.orm import Session

from .config import get_settings
from .crypto_util import hash_audit_chain
from .data.models import AlertConfig, AuditLog, UsageDaily, utcnow
from .auth.rate_limit import rate_limiter

_alert_lock = threading.Lock()
_last_alert: dict[str, float] = {}

GENESIS = "0" * 64


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
    payload = "|".join(
        [
            created.isoformat(),
            str(uid or ""),
            username,
            action,
            entity_type,
            entity_s,
            detail_s,
        ]
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
            payload = "|".join(
                [
                    row.created_at.isoformat() if row.created_at else "",
                    str(row.actor_user_id or ""),
                    row.actor_username or "",
                    row.action or "",
                    row.entity_type or "",
                    row.entity_id or "",
                    row.detail or "",
                ]
            )
            expected = hash_audit_chain(row.prev_hash or GENESIS, payload)
            if row.prev_hash and expected != row.entry_hash:
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
        if duration_ms is not None:
            row.latency_sum_ms = float(row.latency_sum_ms or 0) + float(duration_ms)
            row.latency_count = (row.latency_count or 0) + 1
    elif result == "rate_limit":
        row.rate_limit_count += 1
    else:
        row.deny_count += 1


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
    payload = {"text": f"[LLM-Gateway] {event}: {message}", "event": event, "message": message}
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
) -> None:
    pct = rate_limiter.quota_usage_pct(
        key_id=key_id, team_id=team_id, key_q=key_q, team_q=team_q
    )
    if pct is None:
        return
    maybe_alert(
        db,
        event="quota",
        message=f"key={label} usage={pct:.0f}% of daily quota",
        quota_pct=pct,
    )
