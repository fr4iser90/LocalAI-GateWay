from __future__ import annotations

from fastapi import Depends, Request
from sqlalchemy.orm import Session, joinedload
from typing import Annotated

from ..data.db import get_db
from ..data.models import AdminUser, ApiKey, Team, TeamMember


class RedirectToLogin(Exception):
    pass


class Forbidden(Exception):
    pass


class SetupWizardRequired(Exception):
    """Platform admin must finish first-run wizard before using the rest of the UI."""

    def __init__(self, redirect_to: str):
        self.redirect_to = redirect_to


def _setup_path_allowed(path: str) -> bool:
    """Routes reachable while the first-run wizard is incomplete."""
    p = path or "/"
    if p.startswith("/setup"):
        return True
    if p.startswith("/account"):
        return True
    if p.startswith("/static"):
        return True
    if p in {"/logout", "/login", "/forgot", "/register", "/healthz"}:
        return True
    return False


def current_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> AdminUser | None:
    uid = request.session.get("user_id")
    if not uid:
        return None
    user = (
        db.query(AdminUser)
        .options(joinedload(AdminUser.memberships).joinedload(TeamMember.team))
        .filter(AdminUser.id == uid)
        .first()
    )
    if user is None or not user.is_active:
        return None
    return user


def require_user(
    request: Request, db: Annotated[Session, Depends(get_db)]
) -> AdminUser:
    user = current_user(request, db)
    if user is None:
        raise RedirectToLogin()

    # First-run: lock the full admin UI until sources → models → key are done.
    if user.is_platform_admin:
        from .setup import needs_setup_wizard, wizard_progress

        if needs_setup_wizard(db, user):
            request.state.setup_incomplete = True
            if not _setup_path_allowed(request.url.path):
                nxt = wizard_progress(db)["next"]
                raise SetupWizardRequired((nxt["path"] if nxt else "/setup"))
        else:
            request.state.setup_incomplete = False

    return user


def require_platform_admin(user: Annotated[AdminUser, Depends(require_user)]) -> AdminUser:
    if not user.is_platform_admin:
        raise Forbidden()
    return user


def user_team_ids(user: AdminUser) -> set[int]:
    return {m.team_id for m in user.memberships}


def user_teams(user: AdminUser) -> list[Team]:
    return [m.team for m in user.memberships if m.team]


def can_access_team(user: AdminUser, team_id: int | None) -> bool:
    if user.is_platform_admin:
        return True
    if team_id is None:
        return False
    return team_id in user_team_ids(user)


def can_access_key(user: AdminUser, api_key, *, teams_enabled: bool) -> bool:
    """Keys: team membership when teams on, else owner-only isolation."""
    if user.is_platform_admin:
        return True
    if teams_enabled:
        return can_access_team(user, api_key.team_id)
    return api_key.owner_user_id == user.id


def scope_keys_query(q, user: AdminUser, *, teams_enabled: bool):
    if user.is_platform_admin:
        return q
    if teams_enabled:
        tids = user_team_ids(user)
        return q.filter(ApiKey.team_id.in_(tids)) if tids else q.filter(False)
    return q.filter(ApiKey.owner_user_id == user.id)


def is_team_owner(user: AdminUser, team_id: int) -> bool:
    if user.is_platform_admin:
        return True
    for m in user.memberships:
        if m.team_id == team_id and m.role == "owner":
            return True
    return False
