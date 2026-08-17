from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ..audit import write_audit
from ..config import get_settings
from ..crypto_util import encrypt_secret
from ..data.db import get_db, hash_password, verify_password
from ..password_policy import policy_for_template, validate_new_password
from ..mailer import MailError, send_mail, smtp_ready, get_smtp
from ..data.models import AdminUser, AuthSettings, PasswordResetToken, SmtpConfig, Team, TeamMember, utcnow
from .access import Forbidden, require_platform_admin, require_user, current_user
from .templating import make_templates

templates = make_templates()
router = APIRouter()

RESET_TTL = timedelta(hours=1)


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_reset_token(
    db: Session, user: AdminUser, *, by_admin: bool = False
) -> str:
    raw = secrets.token_urlsafe(32)
    db.query(PasswordResetToken).filter(
        PasswordResetToken.user_id == user.id,
        PasswordResetToken.used_at.is_(None),
    ).delete()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=_token_hash(raw),
            expires_at=utcnow() + RESET_TTL,
            created_by_admin=by_admin,
        )
    )
    return raw


def find_user_by_login(db: Session, login: str) -> AdminUser | None:
    login = (login or "").strip()
    if not login:
        return None
    if "@" in login:
        return (
            db.query(AdminUser)
            .filter(AdminUser.email == login.lower())
            .first()
        )
    return db.query(AdminUser).filter(AdminUser.username == login).first()


def get_auth_settings(db: Session) -> AuthSettings:
    cfg = db.query(AuthSettings).first()
    if cfg is None:
        cfg = AuthSettings(
            allow_self_registration=False,
            require_email=True,
            teams_enabled=False,
            anonymize_client_ip=True,
            retention_days=30,
            auto_vl_routing=False,
            auto_model_default="",
            auto_model_quality="",
            auto_model_long="",
            max_keys_per_user=3,
            pool_window_hours=5,
            pool_tokens_per_unit=1,
            pool_min_cost=1.0,
            pool_watt_weight=0.0,
            pool_tokens_per_sec=50.0,
            pool_model_weights_enabled=False,
        )
        db.add(cfg)
        db.flush()
    return cfg


def teams_feature_enabled(db: Session) -> bool:
    return bool(get_auth_settings(db).teams_enabled)


def active_key_count(db: Session, owner_user_id: int) -> int:
    from ..data.models import ApiKey

    return (
        db.query(ApiKey)
        .filter(
            ApiKey.owner_user_id == owner_user_id,
            ApiKey.is_active.is_(True),
        )
        .count()
    )


def max_keys_allowed(db: Session, owner: AdminUser | None) -> int | None:
    """None = unlimited. Platform admins always unlimited."""
    if owner is None or owner.is_platform_admin:
        return None
    n = int(getattr(get_auth_settings(db), "max_keys_per_user", 3) or 0)
    return None if n <= 0 else n


def assert_can_create_key(db: Session, owner: AdminUser | None) -> str | None:
    """Return error message if owner is at max keys, else None."""
    limit = max_keys_allowed(db, owner)
    if limit is None or owner is None:
        return None
    have = active_key_count(db, owner.id)
    if have >= limit:
        return (
            f"User '{owner.username}' already has {have} active key(s) "
            f"(limit {limit}). Raise Max keys in Settings or revoke an old key."
        )
    return None


# ---- Login helpers used from routes (re-export pattern via import) ----


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if current_user(request, db):
        return RedirectResponse("/me", status_code=303)
    auth = get_auth_settings(db)
    if not auth.allow_self_registration:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Self-registration is disabled. Ask an admin for an account.",
                "enabled": False,
                "require_email": auth.require_email,
            },
            status_code=403,
        )
    return templates.TemplateResponse(
        request,
        "register.html",
        {
            "error": None,
            "enabled": True,
            "require_email": auth.require_email,
            "pw_policy": policy_for_template(),
        },
    )


@router.post("/register")
def register_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    email: str = Form(""),
):
    auth = get_auth_settings(db)
    if not auth.allow_self_registration:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Self-registration is disabled.",
                "enabled": False,
                "require_email": auth.require_email,
            },
            status_code=403,
        )

    client_ip = request.client.host if request.client else "unknown"
    # reuse forgot limiter buckets (5/h IP) under a register: prefix via login key
    allowed, limit_msg = forgot_limiter.allow(ip=client_ip, login=f"register:{username}")
    if not allowed:
        return templates.TemplateResponse(
            request,
            "register.html",
            {"error": limit_msg, "enabled": True, "require_email": auth.require_email},
            status_code=429,
        )

    username = username.strip()
    email_n = email.strip().lower() or None
    if len(username) < 3:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Username must be at least 3 characters.",
                "enabled": True,
                "require_email": auth.require_email,
            },
            status_code=400,
        )
    err = validate_new_password(password, password2)
    if err:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": err,
                "enabled": True,
                "require_email": auth.require_email,
                "pw_policy": policy_for_template(),
            },
            status_code=400,
        )
    if auth.require_email and not email_n:
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Email is required.",
                "enabled": True,
                "require_email": True,
            },
            status_code=400,
        )
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Username already taken.",
                "enabled": True,
                "require_email": auth.require_email,
            },
            status_code=400,
        )
    if email_n and db.query(AdminUser).filter(AdminUser.email == email_n).first():
        return templates.TemplateResponse(
            request,
            "register.html",
            {
                "error": "Email already in use.",
                "enabled": True,
                "require_email": auth.require_email,
            },
            status_code=400,
        )

    user = AdminUser(
        username=username,
        email=email_n,
        password_hash=hash_password(password),
        is_active=True,
        is_platform_admin=False,
        must_change_password=False,
    )
    db.add(user)
    db.flush()
    if auth.teams_enabled and auth.default_team_id:
        team = db.get(Team, auth.default_team_id)
        if team:
            db.add(TeamMember(team_id=team.id, user_id=user.id, role="member"))
    elif not auth.teams_enabled:
        from ..data.backends import default_grant_source_names
        from ..data.grants import sync_user_grants

        sync_user_grants(db, user, default_grant_source_names(db))
    write_audit(
        db,
        actor=user,
        action="auth.register",
        entity_type="user",
        entity_id=user.id,
        detail=username,
    )
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/me", status_code=303)


@router.get("/forgot", response_class=HTMLResponse)
def forgot_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if current_user(request, db):
        return RedirectResponse("/me", status_code=303)
    cfg = get_smtp(db)
    return templates.TemplateResponse(
        request,
        "forgot.html",
        {"error": None, "ok": None, "smtp_ok": smtp_ready(cfg)},
    )


@router.post("/forgot")
def forgot_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    login: str = Form(...),
):
    cfg = get_smtp(db)
    client_ip = request.client.host if request.client else "unknown"
    allowed, limit_msg = forgot_limiter.allow(ip=client_ip, login=login)
    if not allowed:
        return templates.TemplateResponse(
            request,
            "forgot.html",
            {"error": limit_msg, "ok": None, "smtp_ok": smtp_ready(cfg)},
            status_code=429,
        )

    # Always show same message (no user enumeration)
    ok_msg = "If an account with that username/email exists and SMTP is configured, a reset link was sent."
    if not smtp_ready(cfg):
        return templates.TemplateResponse(
            request,
            "forgot.html",
            {
                "error": "Password reset is unavailable: admin has not configured SMTP yet.",
                "ok": None,
                "smtp_ok": False,
            },
            status_code=503,
        )

    user = find_user_by_login(db, login)
    if user and user.is_active and user.email:
        try:
            raw = create_reset_token(db, user, by_admin=False)
            assert cfg is not None
            link = f"{cfg.public_base_url.rstrip('/')}/reset?token={raw}"
            send_mail(
                db,
                to_email=user.email,
                subject="Password reset — LocalAI Gateway",
                body_text=(
                    f"Hi {user.username},\n\n"
                    f"Reset your password (valid 1 hour):\n{link}\n\n"
                    f"If you did not request this, ignore this email.\n"
                ),
            )
            write_audit(
                db,
                actor=None,
                action="auth.forgot",
                entity_type="user",
                entity_id=user.id,
                detail="reset_mail_sent",
            )
            db.commit()
        except MailError as exc:
            db.rollback()
            return templates.TemplateResponse(
                request,
                "forgot.html",
                {"error": f"Could not send mail: {exc}", "ok": None, "smtp_ok": True},
                status_code=502,
            )
    else:
        db.commit()

    return templates.TemplateResponse(
        request, "forgot.html", {"error": None, "ok": ok_msg, "smtp_ok": True}
    )


@router.get("/reset", response_class=HTMLResponse)
def reset_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: str = "",
):
    return templates.TemplateResponse(
        request,
        "reset.html",
        {"token": token, "error": None, "pw_policy": policy_for_template()},
    )


@router.post("/reset")
def reset_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    token: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    err = validate_new_password(password, password2)
    if err:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {
                "token": token,
                "error": err,
                "pw_policy": policy_for_template(),
            },
            status_code=400,
        )
    row = (
        db.query(PasswordResetToken)
        .filter(PasswordResetToken.token_hash == _token_hash(token))
        .first()
    )
    if (
        row is None
        or row.used_at is not None
        or row.expires_at < utcnow()
    ):
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"token": token, "error": "Invalid or expired reset link."},
            status_code=400,
        )
    user = db.get(AdminUser, row.user_id)
    if user is None or not user.is_active:
        return templates.TemplateResponse(
            request,
            "reset.html",
            {"token": token, "error": "Invalid or expired reset link."},
            status_code=400,
        )
    user.password_hash = hash_password(password)
    user.must_change_password = False
    row.used_at = utcnow()
    write_audit(
        db,
        actor=user,
        action="auth.reset",
        entity_type="user",
        entity_id=user.id,
        detail="password_reset",
    )
    db.commit()
    return RedirectResponse("/login?reset=1", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    flash = request.session.pop("flash_ok", None)
    err = request.session.pop("flash_err", None)
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "user": user,
            "nav": "account",
            "flash_ok": flash,
            "flash_err": err,
            "is_admin": user.is_platform_admin,
            "force_pw": user.must_change_password,
            "pw_policy": policy_for_template(),
        },
    )


@router.post("/account/timezone/auto")
async def account_timezone_auto(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    """Silent browser → store. No UI, no flash. Called from base.html."""
    from fastapi.responses import JSONResponse

    from ..stats import is_valid_timezone

    raw = ""
    ctype = (request.headers.get("content-type") or "").lower()
    if "application/json" in ctype:
        try:
            body = await request.json()
            raw = str((body or {}).get("timezone") or "").strip()
        except Exception:
            raw = ""
    else:
        form = await request.form()
        raw = str(form.get("timezone") or "").strip()

    if not is_valid_timezone(raw):
        return JSONResponse({"ok": False, "error": "invalid"}, status_code=400)

    if (user.timezone or "") != raw:
        user.timezone = raw
        db.commit()

    resp = JSONResponse({"ok": True, "timezone": raw})
    resp.set_cookie(
        key="gw_tz",
        value=raw,
        max_age=60 * 60 * 24 * 365,
        httponly=False,
        samesite="lax",
        path="/",
    )
    return resp


@router.post("/account/password")
def account_password(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
    current_password: str = Form(""),
    password: str = Form(...),
    password2: str = Form(...),
):
    err = validate_new_password(password, password2)
    if err:
        request.session["flash_err"] = err
        return RedirectResponse("/account", status_code=303)
    if not user.must_change_password:
        if not verify_password(current_password, user.password_hash):
            request.session["flash_err"] = "Current password is wrong."
            return RedirectResponse("/account", status_code=303)
    user.password_hash = hash_password(password)
    user.must_change_password = False
    write_audit(
        db, actor=user, action="auth.password_change", entity_type="user", entity_id=user.id
    )
    db.commit()
    request.session["flash_ok"] = "Password updated."
    return RedirectResponse("/account", status_code=303)


@router.post("/account/email")
def account_email(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
    email: str = Form(""),
    current_password: str = Form(...),
):
    if not verify_password(current_password, user.password_hash):
        request.session["flash_err"] = "Current password is wrong."
        return RedirectResponse("/account", status_code=303)
    email = email.strip().lower()
    if email:
        other = db.query(AdminUser).filter(AdminUser.email == email, AdminUser.id != user.id).first()
        if other:
            request.session["flash_err"] = "Email already in use."
            return RedirectResponse("/account", status_code=303)
        user.email = email
    else:
        user.email = None
    write_audit(
        db,
        actor=user,
        action="auth.email_change",
        entity_type="user",
        entity_id=user.id,
        detail=email or "(cleared)",
    )
    db.commit()
    request.session["flash_ok"] = "Email updated."
    return RedirectResponse("/account", status_code=303)


@router.get("/privacy", response_class=HTMLResponse)
def privacy_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    auth = get_auth_settings(db)
    flash = request.session.pop("flash_ok", None)
    err = request.session.pop("flash_err", None)
    return templates.TemplateResponse(
        request,
        "privacy.html",
        {
            "user": user,
            "nav": "privacy",
            "auth": auth,
            "flash_ok": flash,
            "flash_err": err,
            "is_admin": user.is_platform_admin,
        },
    )


@router.post("/privacy/wipe-usage")
def privacy_wipe_usage(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
    confirm: str = Form(""),
    current_password: str = Form(...),
):
    from ..data.models import ApiKey
    from ..privacy import wipe_usage_for_keys

    if not verify_password(current_password, user.password_hash):
        request.session["flash_err"] = "Password incorrect."
        return RedirectResponse("/privacy", status_code=303)
    if confirm.strip().upper() != "DELETE":
        request.session["flash_err"] = "Type DELETE to confirm."
        return RedirectResponse("/privacy", status_code=303)

    key_ids = [
        kid
        for (kid,) in db.query(ApiKey.id)
        .filter(ApiKey.owner_user_id == user.id)
        .all()
    ]
    n = wipe_usage_for_keys(db, key_ids)
    write_audit(
        db,
        actor=user,
        action="privacy.wipe_usage",
        entity_type="user",
        entity_id=user.id,
        detail=f"events={n} keys={len(key_ids)}",
    )
    db.commit()
    request.session["flash_ok"] = f"Deleted {n} usage events for your keys."
    return RedirectResponse("/privacy", status_code=303)


# ---- Admin SMTP ----


@router.get("/smtp", response_class=HTMLResponse)
def smtp_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    cfg = get_smtp(db)
    flash = request.session.pop("flash_ok", None)
    err = request.session.pop("flash_err", None)
    return templates.TemplateResponse(
        request,
        "smtp.html",
        {
            "user": user,
            "cfg": cfg,
            "nav": "smtp",
            "is_admin": True,
            "flash_ok": flash,
            "flash_err": err,
            "ready": smtp_ready(cfg),
        },
    )


@router.post("/smtp")
async def smtp_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    form = await request.form()
    cfg = get_smtp(db) or SmtpConfig()
    if cfg.id is None:
        db.add(cfg)
    cfg.enabled = form.get("enabled") == "on"
    cfg.host = str(form.get("host") or "").strip()
    cfg.port = int(form.get("port") or 587)
    cfg.username = str(form.get("username") or "").strip()
    cfg.from_email = str(form.get("from_email") or "").strip()
    cfg.from_name = str(form.get("from_name") or "LocalAI Gateway").strip()
    cfg.use_tls = form.get("use_tls") == "on"
    cfg.use_ssl = form.get("use_ssl") == "on"
    cfg.public_base_url = str(form.get("public_base_url") or "").strip().rstrip("/")
    new_pw = str(form.get("password") or "")
    if new_pw:
        cfg.password = encrypt_secret(new_pw, get_settings().session_secret)
    elif cfg.password and not cfg.password.startswith("enc:"):
        # migrate legacy plaintext on save
        cfg.password = encrypt_secret(cfg.password, get_settings().session_secret)
    write_audit(db, actor=user, action="smtp.update", entity_type="smtp_config", detail=cfg.host)
    db.commit()
    request.session["flash_ok"] = "SMTP settings saved (password stored encrypted)."
    return RedirectResponse("/smtp", status_code=303)


@router.post("/smtp/test")
def smtp_test(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    to_email: str = Form(""),
):
    to = (to_email or user.email or "").strip()
    if not to:
        request.session["flash_err"] = "Set a destination email (or your user email)."
        return RedirectResponse("/smtp", status_code=303)
    try:
        send_mail(
            db,
            to_email=to,
            subject="LocalAI Gateway SMTP test",
            body_text=f"SMTP test OK from LocalAI Gateway (sent by {user.username}).\n",
        )
        write_audit(db, actor=user, action="smtp.test", detail=to)
        db.commit()
        request.session["flash_ok"] = f"Test mail sent to {to}."
    except MailError as exc:
        request.session["flash_err"] = f"SMTP test failed: {exc}"
    return RedirectResponse("/smtp", status_code=303)
