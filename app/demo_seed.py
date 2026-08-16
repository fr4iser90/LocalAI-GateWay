"""Demo usage seed for dashboard visualization."""

from __future__ import annotations

import random
from datetime import timedelta

from sqlalchemy.orm import Session

from .audit import bump_usage_daily
from .data.models import ApiKey, UsageDaily, UsageEvent, utcnow

SERVICES_DEMO = [
    ("chat", ["/v1/chat/completions", "llama3.2"], "tokens"),
    ("embed", ["/v1/embeddings", "nomic-embed-text"], "tokens"),
    ("stt", ["/v1/audio/transcriptions", "whisper-large"], "audio"),
    ("tts", ["/v1/audio/speech", "piper-en"], "tts"),
]

RESULTS = ["ok"] * 85 + ["deny"] * 8 + ["rate_limit"] * 7


def clear_demo_usage(db: Session) -> int:
    n = db.query(UsageEvent).filter(UsageEvent.is_demo.is_(True)).delete()
    # Rebuild daily from remaining events is expensive; drop daily rows that only
    # came from demo by resetting counters for today-7 — simpler: delete daily
    # rows with zero real events for recent days after demo clear is best-effort.
    # Recompute last 14 days daily from events.
    _recompute_daily(db, days=14)
    return int(n or 0)


def _recompute_daily(db: Session, *, days: int = 14) -> None:
    cutoff = utcnow() - timedelta(days=days)
    db.query(UsageDaily).filter(UsageDaily.day >= cutoff.date()).delete()
    events = (
        db.query(UsageEvent)
        .filter(UsageEvent.created_at >= cutoff)
        .order_by(UsageEvent.created_at.asc())
        .all()
    )
    for e in events:
        bump_usage_daily(
            db,
            team_id=e.team_id,
            api_key_id=e.api_key_id,
            team_name=e.team_name,
            key_label=e.key_label,
            service=e.service,
            model=e.model,
            result=e.result,
            tokens_in=e.tokens_in,
            tokens_out=e.tokens_out,
            audio_seconds=e.audio_seconds,
            response_chars=e.response_chars,
            duration_ms=e.duration_ms,
            day=e.created_at.date() if e.created_at else None,
        )


def seed_demo_usage(db: Session, *, count: int = 120) -> int:
    """Insert synthetic usage events tagged is_demo=True (replaces prior demo)."""
    clear_demo_usage(db)
    keys = db.query(ApiKey).filter(ApiKey.is_active.is_(True)).all()
    if not keys:
        keys = [None]

    now = utcnow()
    created = 0
    for i in range(count):
        svc, (path, model), kind = random.choice(SERVICES_DEMO)
        key = random.choice(keys)
        result = random.choice(RESULTS)
        # Spread across the 7-day window so every day has samples
        if i < 21:
            day_offset = i % 7
            age_h = day_offset * 24 + random.uniform(1, 20)
        else:
            age_h = random.uniform(0, 24 * 6.5)
        created_at = now - timedelta(hours=age_h)
        duration = random.uniform(40, 2800) if result == "ok" else random.uniform(5, 120)

        tokens_in = tokens_out = None
        audio_seconds = response_chars = None
        if result == "ok":
            if kind == "tokens":
                tokens_in = random.randint(80, 2500)
                tokens_out = random.randint(20, 900) if svc != "embed" else 0
            elif kind == "audio":
                audio_seconds = round(random.uniform(1.5, 90.0), 2)
            elif kind == "tts":
                response_chars = random.randint(40, 1200)
                audio_seconds = round(response_chars / 14.0, 2)

        ip = f"10.0.{random.randint(0, 5)}.{random.randint(1, 254)}"
        db.add(
            UsageEvent(
                created_at=created_at,
                api_key_id=key.id if key else None,
                team_id=key.team_id if key else None,
                key_label=key.label if key else "demo-key",
                team_name=key.team.name if key and key.team else "",
                service=svc,
                method="POST",
                path=path,
                host=f"{svc}.demo.local",
                client_ip=ip,
                model=model,
                status=204 if result == "ok" else (429 if result == "rate_limit" else 403),
                result=result,
                duration_ms=duration,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                audio_seconds=audio_seconds,
                response_chars=response_chars,
                is_demo=True,
            )
        )
        created += 1
        if result:
            bump_usage_daily(
                db,
                team_id=key.team_id if key else None,
                api_key_id=key.id if key else None,
                team_name=key.team.name if key and key.team else "",
                key_label=key.label if key else "demo-key",
                service=svc,
                model=model,
                result=result,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                audio_seconds=audio_seconds,
                response_chars=response_chars,
                duration_ms=duration,
                day=created_at.date(),
            )
    return created
