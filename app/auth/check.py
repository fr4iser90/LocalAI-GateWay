from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..audit import bump_usage_daily, check_quota_alert, maybe_alert
from ..config import MODEL_CHECK_KINDS, get_settings
from ..data.db import hash_api_key
from ..data.models import AdminUser, ApiKey, ModelLimit, Team, UsageDaily, UsageEvent, utcnow
from .priority import priority_gate
from .rate_limit import rate_limiter


@dataclass
class AuthResult:
    status: int
    reason: str = "ok"
    remaining_rpm: int | None = None
    priority: int = 0
    api_key: ApiKey | None = None
    model: str | None = None


def check_temperature() -> AuthResult | None:
    settings = get_settings()
    if settings.temp_guard_disabled:
        return None
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(settings.temp_guard_url)
    except Exception as exc:
        return AuthResult(status=503, reason=f"temp_guard_unreachable:{exc}")

    if resp.status_code == 204:
        return None
    if resp.status_code == 403:
        return AuthResult(status=503, reason="local_temperature_above_limit")
    return AuthResult(status=503, reason=f"temp_guard_status_{resp.status_code}")


def extract_model(body: bytes | None, content_type: str | None) -> str | None:
    if not body:
        return None
    ct = (content_type or "").lower()
    if "multipart/form-data" in ct:
        # Whisper-style: form field "model"
        m = re.search(
            rb'Content-Disposition:[^\n]*name="model"[^\n]*\r?\n\r?\n([^\r\n]+)',
            body[:65536],
            re.IGNORECASE,
        )
        if m:
            return m.group(1).decode("utf-8", errors="ignore").strip() or None
        return None
    if "application/json" not in ct and body[:1] not in (b"{", b"["):
        return None
    try:
        payload = json.loads(body[:65536].decode("utf-8", errors="ignore"))
    except Exception:
        return None
    if isinstance(payload, dict):
        model = payload.get("model")
        if isinstance(model, str):
            return model
    return None


def _services_for_key(api_key: ApiKey, db: Session | None = None) -> set[str]:
    """Effective sources for a key (grant ceiling ∩ key subset / inherit)."""
    if db is None:
        # Legacy fallback without ceiling (tests that don't pass db)
        key_services = {g.service for g in api_key.service_grants}
        if key_services:
            return key_services
        if api_key.team:
            return {g.service for g in api_key.team.service_grants}
        return set()
    from ..data.grants import effective_services

    return effective_services(db, api_key)


def _models_for_key(
    api_key: ApiKey, service: str, db: Session | None = None
) -> set[str] | None:
    if db is None:
        key_models = [m.model_name for m in api_key.model_allowlists if m.service == service]
        if key_models:
            return set(key_models)
        if api_key.team:
            team_models = [
                m.model_name for m in api_key.team.model_allowlists if m.service == service
            ]
            if team_models:
                return set(team_models)
        return None
    from ..data.grants import effective_models

    return effective_models(db, api_key, service)


def _pick_model_limit(api_key: ApiKey, service: str, model: str | None) -> ModelLimit | None:
    if not model:
        return None
    for lim in api_key.model_limits:
        if lim.service == service and lim.model_name == model:
            return lim
    if api_key.team:
        for lim in api_key.team.model_limits:
            if lim.service == service and lim.model_name == model:
                return lim
    return None


def _effective_limits(api_key: ApiKey) -> tuple[int | None, int | None, int, int | None, int | None]:
    rpm = api_key.rpm_limit
    concurrency = api_key.concurrency_limit
    priority = api_key.priority if api_key.priority is not None else 0
    daily = api_key.daily_quota
    monthly = None
    if api_key.team:
        if rpm is None:
            rpm = api_key.team.rpm_limit
        if concurrency is None:
            concurrency = api_key.team.concurrency_limit
        if api_key.priority is None:
            priority = api_key.team.priority or 0
        if daily is None:
            daily = api_key.team.daily_quota
        monthly = api_key.team.monthly_quota
    return rpm, concurrency, priority, daily, monthly


def _monthly_ok_count(db: Session, team_id: int | None) -> int:
    if not team_id:
        return 0
    today = utcnow().date()
    month_start = today.replace(day=1)
    total = (
        db.query(func.coalesce(func.sum(UsageDaily.ok_count), 0))
        .filter(UsageDaily.team_id == team_id, UsageDaily.day >= month_start)
        .scalar()
    )
    return int(total or 0)


def log_usage(
    db: Session,
    *,
    api_key: ApiKey | None,
    service: str,
    method: str,
    path: str,
    host: str,
    client_ip: str,
    model: str | None,
    status: int,
    result: str,
    duration_ms: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    audio_seconds: float | None = None,
    response_chars: int | None = None,
    body: bytes | None = None,
    kind: str | None = None,
) -> None:
    from ..admin.accounts import get_auth_settings
    from ..privacy import anonymize_ip, estimate_prompt_tokens

    auth = get_auth_settings(db)
    ip = client_ip or ""
    if auth.anonymize_client_ip:
        ip = anonymize_ip(ip)

    if tokens_in is None and result == "ok" and kind in MODEL_CHECK_KINDS:
        tokens_in = estimate_prompt_tokens(body)

    team_id = api_key.team_id if api_key else None
    key_id = api_key.id if api_key else None
    key_label = api_key.label if api_key else ""
    team_name = api_key.team.name if api_key and api_key.team else ""
    db.add(
        UsageEvent(
            api_key_id=key_id,
            team_id=team_id,
            key_label=key_label,
            team_name=team_name,
            service=service,
            method=method,
            path=path[:512],
            host=host[:256],
            client_ip=ip[:64],
            model=model,
            status=status,
            result=result,
            duration_ms=duration_ms,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            audio_seconds=audio_seconds,
            response_chars=response_chars,
            is_demo=False,
        )
    )
    bump_usage_daily(
        db,
        team_id=team_id,
        api_key_id=key_id,
        team_name=team_name,
        key_label=key_label,
        service=service,
        model=model,
        result=result,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        audio_seconds=audio_seconds,
        response_chars=response_chars,
        duration_ms=duration_ms,
    )
    if result == "rate_limit":
        maybe_alert(
            db,
            event="rate_limit",
            message=f"key={key_label or '?'} service={service} model={model or '-'}",
        )


def authorize(
    db: Session,
    *,
    raw_key: str | None,
    service: str,
    kind: str,
    method: str,
    path: str,
    host: str,
    client_ip: str,
    body: bytes | None = None,
    content_type: str | None = None,
) -> AuthResult:
    started = time.perf_counter()
    model = None
    if kind in MODEL_CHECK_KINDS:
        model = extract_model(body, content_type)

    def _fail(status: int, reason: str, api_key: ApiKey | None = None, **kw) -> AuthResult:
        log_usage(
            db,
            api_key=api_key,
            service=service,
            kind=kind,
            method=method,
            path=path,
            host=host,
            client_ip=client_ip,
            model=model,
            status=status,
            result="rate_limit" if status == 429 else "deny",
            duration_ms=(time.perf_counter() - started) * 1000,
            body=body,
        )
        db.commit()
        return AuthResult(status=status, reason=reason, api_key=api_key, model=model, **kw)

    if not raw_key:
        return _fail(401, "missing_api_key")

    key_hash = hash_api_key(raw_key)
    api_key = (
        db.query(ApiKey)
        .options(
            joinedload(ApiKey.team).joinedload(Team.service_grants),
            joinedload(ApiKey.team).joinedload(Team.model_allowlists),
            joinedload(ApiKey.team).joinedload(Team.model_limits),
            joinedload(ApiKey.owner).joinedload(AdminUser.service_grants),
            joinedload(ApiKey.owner).joinedload(AdminUser.model_allowlists),
            joinedload(ApiKey.service_grants),
            joinedload(ApiKey.model_allowlists),
            joinedload(ApiKey.model_limits),
        )
        .filter(ApiKey.key_hash == key_hash)
        .first()
    )

    if api_key is None or not api_key.is_active:
        return _fail(401, "invalid_api_key")

    if api_key.expires_at and api_key.expires_at < utcnow():
        return _fail(401, "expired_api_key", api_key)

    if service not in _services_for_key(api_key, db):
        return _fail(403, "service_not_allowed", api_key)

    if kind in MODEL_CHECK_KINDS and model:
        allow = _models_for_key(api_key, service, db)
        if allow is not None and model not in allow:
            return _fail(403, "model_not_allowed", api_key)
        from ..data.catalog import is_model_globally_enabled

        if not is_model_globally_enabled(db, service, model):
            return _fail(403, "model_disabled", api_key)

    rpm, concurrency, priority, daily_quota, monthly_quota = _effective_limits(api_key)
    mlim = _pick_model_limit(api_key, service, model)

    if monthly_quota and monthly_quota > 0 and api_key.team_id:
        if _monthly_ok_count(db, api_key.team_id) >= monthly_quota:
            return _fail(429, "team_monthly_quota_exceeded", api_key, priority=priority)

    if not priority_gate.acquire(key_id=api_key.id, priority=priority, timeout=2.0):
        return _fail(429, "priority_queue_timeout", api_key, priority=priority)

    decision = rate_limiter.check_and_acquire(
        key_id=api_key.id,
        team_id=api_key.team_id,
        rpm=rpm,
        concurrency=concurrency,
        model=model,
        model_rpm=mlim.rpm_limit if mlim else None,
        model_concurrency=mlim.concurrency_limit if mlim else None,
        key_daily_quota=api_key.daily_quota,
        team_daily_quota=api_key.team.daily_quota if api_key.team else None,
        model_daily_quota=mlim.daily_quota if mlim else None,
    )
    if not decision.allowed:
        priority_gate.release(api_key.id)
        return _fail(
            429,
            decision.reason,
            api_key,
            remaining_rpm=decision.remaining_rpm,
            priority=priority,
        )

    rate_limiter.release(api_key.id, model=model)
    priority_gate.release(api_key.id)

    temp_block = check_temperature()
    if temp_block is not None:
        return _fail(temp_block.status, temp_block.reason, api_key, priority=priority)

    api_key.last_used_at = utcnow()
    log_usage(
        db,
        api_key=api_key,
        service=service,
        kind=kind,
        method=method,
        path=path,
        host=host,
        client_ip=client_ip,
        model=model,
        status=204,
        result="ok",
        duration_ms=(time.perf_counter() - started) * 1000,
        body=body,
    )
    check_quota_alert(
        db,
        key_id=api_key.id,
        team_id=api_key.team_id,
        key_q=api_key.daily_quota,
        team_q=api_key.team.daily_quota if api_key.team else None,
        label=api_key.label,
    )
    db.commit()

    return AuthResult(
        status=204,
        reason="ok",
        remaining_rpm=decision.remaining_rpm,
        priority=priority,
        api_key=api_key,
        model=model,
    )
