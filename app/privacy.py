"""Privacy helpers: IP anonymization, retention purge, usage wipe."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy.orm import Session

from .data.models import AuthSettings, UsageDaily, UsageEvent, utcnow


def anonymize_ip(ip: str) -> str:
    ip = (ip or "").strip()
    if not ip:
        return ""
    if "." in ip and ":" not in ip:
        parts = ip.split(".")
        if len(parts) == 4:
            return ".".join(parts[:3] + ["0"])
    if ":" in ip:
        # truncate IPv6 to /64-ish
        parts = ip.split(":")
        return ":".join(parts[:4] + ["0"] * max(0, 8 - 4))
    return ip


def estimate_prompt_tokens(body: bytes | None) -> int | None:
    """Rough chars/4 estimate from request body (not billing-grade)."""
    if not body:
        return None
    # avoid counting huge uploads (audio) as tokens
    if len(body) > 200_000:
        return None
    try:
        text = body.decode("utf-8", errors="ignore")
    except Exception:
        return None
    if not text.strip():
        return None
    return max(1, len(text) // 4)


def purge_old_usage(db: Session, retention_days: int) -> int:
    if retention_days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=retention_days)
    n = db.query(UsageEvent).filter(UsageEvent.created_at < cutoff).delete()
    day_cut = cutoff.date()
    db.query(UsageDaily).filter(UsageDaily.day < day_cut).delete()
    return int(n or 0)


def wipe_usage_for_keys(db: Session, key_ids: list[int]) -> int:
    if not key_ids:
        return 0
    n = (
        db.query(UsageEvent)
        .filter(UsageEvent.api_key_id.in_(key_ids))
        .delete(synchronize_session=False)
    )
    db.query(UsageDaily).filter(UsageDaily.api_key_id.in_(key_ids)).delete(
        synchronize_session=False
    )
    return int(n or 0)


def get_privacy_flags(db: Session) -> AuthSettings:
    from .admin.accounts import get_auth_settings

    return get_auth_settings(db)
