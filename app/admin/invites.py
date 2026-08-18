from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta

from sqlalchemy.orm import Session

from ..data.models import AdminUser, RegistrationInvite, utcnow

INVITE_TTL = timedelta(days=7)


def _token_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_registration_invite(
    db: Session,
    *,
    created_by: AdminUser,
    note: str = "",
) -> str:
    raw = secrets.token_urlsafe(32)
    db.add(
        RegistrationInvite(
            token_hash=_token_hash(raw),
            expires_at=utcnow() + INVITE_TTL,
            created_by_id=created_by.id,
            note=(note or "").strip()[:255],
        )
    )
    return raw


def lookup_valid_invite(db: Session, raw: str | None) -> RegistrationInvite | None:
    token = (raw or "").strip()
    if not token:
        return None
    inv = (
        db.query(RegistrationInvite)
        .filter(
            RegistrationInvite.token_hash == _token_hash(token),
            RegistrationInvite.used_at.is_(None),
            RegistrationInvite.expires_at > utcnow(),
        )
        .first()
    )
    return inv


def consume_invite(db: Session, invite: RegistrationInvite, user: AdminUser) -> None:
    invite.used_at = utcnow()
    invite.used_by_user_id = user.id


def invite_register_url(base_url: str, raw: str) -> str:
    return f"{base_url.rstrip('/')}/register?invite={raw}"


def pending_invites(db: Session) -> list[RegistrationInvite]:
    return (
        db.query(RegistrationInvite)
        .filter(
            RegistrationInvite.used_at.is_(None),
            RegistrationInvite.expires_at > utcnow(),
        )
        .order_by(RegistrationInvite.created_at.desc())
        .all()
    )
