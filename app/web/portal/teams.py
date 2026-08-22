from __future__ import annotations

from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ...audit import write_audit
from ...data.backends import source_chip_rows, source_names
from ...data.catalog import list_catalog
from ...data.db import (
    generate_api_key,
    get_db,
    hash_api_key,
    key_display_prefix,
)
from ...data.models import WebUser, ApiKey, Team, TeamMember, UsageEvent, utcnow
from ...data.grants import configured_default_sources
from ..ops import sync_key_model_limits, sync_team_model_limits, _format_model_limits
from ..session import (
    Forbidden,
    can_access_key,
    can_access_team,
    require_platform_admin,
    require_user,
    scope_keys_query,
    scoped_key_ids,
    user_team_ids,
    user_teams,
)
from ..shared import (
    templates,
    _collect_models_from_form,
    _gpu_power_enabled,
    _key_form_context,
    _parse_services,
    _parse_model_checks,
    _resolve_ceiling,
    _selected_model_keys,
    _source_tips,
    _sync_key_grants,
    _sync_key_models,
    _sync_team_grants,
    _sync_team_models,
    _sync_team_members,
    _teams_on,
)

router = APIRouter()


def _require_teams_feature(db: Session) -> None:
    if not _teams_on(db):
        raise Forbidden()


@router.get("/teams", response_class=HTMLResponse)
def teams_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_user)],
):
    _require_teams_feature(db)
    q = db.query(Team).options(
        joinedload(Team.members).joinedload(TeamMember.user),
        joinedload(Team.service_grants),
    )
    if not user.is_platform_admin:
        tids = user_team_ids(user)
        q = q.filter(Team.id.in_(tids)) if tids else q.filter(False)
    teams = q.order_by(Team.name).all()
    return templates.TemplateResponse(
        request,
        "teams.html",
        {
            "user": user,
            "teams": teams,
            "nav": "teams",
            "is_admin": user.is_platform_admin,
            "teams_enabled": True,
        },
    )


@router.get("/teams/new", response_class=HTMLResponse)
def teams_new(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_platform_admin)],
):
    from ...data.grants import AccessCeiling, catalog_groups_for_ceiling
    from ...data.routing_strategy import normalize_routing_strategy, routing_strategy_choices

    _require_teams_feature(db)
    names = source_names(db)
    return templates.TemplateResponse(
        request,
        "team_form.html",
        {
            "user": user,
            "team": None,
            "services": names,
            "source_chips": source_chip_rows(db),
            "selected_services": configured_default_sources(db),
            "catalog_groups": catalog_groups_for_ceiling(
                db, AccessCeiling(unrestricted=True)
            ),
            "routing_strategies": routing_strategy_choices(include_inherit=True),
            "selected_models": set(),
            "model_limits_text": "",
            "users": db.query(WebUser).order_by(WebUser.username).all(),
            "member_ids": [],
            "owner_ids": [],
            "nav": "teams",
            "is_admin": True,
            "read_only": False,
        },
    )


def _apply_team_routing_from_form(team: Team, form, db: Session) -> None:
    from ...data.routing_strategy import normalize_routing_strategy

    names = set(source_names(db))
    raw_strat = (form.get("routing_strategy") or "").strip()
    team.routing_strategy = normalize_routing_strategy(raw_strat) if raw_strat else ""
    pref = str(form.get("preferred_source") or "").strip().lower()
    team.preferred_source = pref if pref in names else ""


@router.post("/teams/new")
async def teams_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_platform_admin)],
):
    _require_teams_feature(db)
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        return RedirectResponse("/teams/new", status_code=303)
    team = Team(
        name=name,
        description=str(form.get("description") or ""),
        rpm_limit=int(form["rpm_limit"]) if form.get("rpm_limit") else None,
        concurrency_limit=int(form["concurrency_limit"])
        if form.get("concurrency_limit")
        else None,
        daily_quota=int(form["daily_quota"]) if form.get("daily_quota") else None,
        monthly_quota=int(form["monthly_quota"]) if form.get("monthly_quota") else None,
        priority=int(form.get("priority") or 0),
    )
    db.add(team)
    db.flush()
    names = source_names(db)
    services = _parse_services(form.getlist("services"), names)
    models = _collect_models_from_form(form, db)
    _sync_team_grants(db, team, services)
    _sync_team_models(db, team, models)
    sync_team_model_limits(db, team, str(form.get("model_limits") or ""))
    _sync_team_members(db, team, form.getlist("members"), form.getlist("owners"))
    _apply_team_routing_from_form(team, form, db)
    write_audit(
        db, actor=user, action="team.create", entity_type="team", entity_id=team.id, detail=name
    )
    db.commit()
    return RedirectResponse("/teams", status_code=303)


@router.get("/teams/{team_id}", response_class=HTMLResponse)
def teams_edit(
    team_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_user)],
):
    _require_teams_feature(db)
    if not can_access_team(user, team_id) and not user.is_platform_admin:
        raise Forbidden()
    team = (
        db.query(Team)
        .options(
            joinedload(Team.service_grants),
            joinedload(Team.model_allowlists),
            joinedload(Team.model_limits),
            joinedload(Team.members).joinedload(TeamMember.user),
        )
        .filter(Team.id == team_id)
        .first()
    )
    if not team:
        return RedirectResponse("/teams", status_code=303)
    selected = _selected_model_keys(team.model_allowlists)
    # Only platform admins see the full user directory for membership editing.
    if user.is_platform_admin:
        users = db.query(WebUser).order_by(WebUser.username).all()
    else:
        users = [m.user for m in team.members if m.user]
    from ...data.grants import AccessCeiling, catalog_groups_for_ceiling
    from ...data.routing_strategy import routing_strategy_choices

    return templates.TemplateResponse(
        request,
        "team_form.html",
        {
            "user": user,
            "team": team,
            "services": source_names(db),
            "source_chips": source_chip_rows(db),
            "selected_services": [g.service for g in team.service_grants],
            "catalog_groups": catalog_groups_for_ceiling(
                db, AccessCeiling(unrestricted=True)
            ),
            "routing_strategies": routing_strategy_choices(include_inherit=True),
            "selected_models": selected,
            "model_limits_text": _format_model_limits(team.model_limits),
            "users": users,
            "member_ids": [m.user_id for m in team.members],
            "owner_ids": [m.user_id for m in team.members if m.role == "owner"],
            "nav": "teams",
            "is_admin": user.is_platform_admin,
            "teams_enabled": True,
            "read_only": not user.is_platform_admin,
        },
    )


@router.post("/teams/{team_id}")
async def teams_update(
    team_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_user)],
):
    _require_teams_feature(db)
    if not user.is_platform_admin:
        raise Forbidden()
    team = db.get(Team, team_id)
    if not team:
        return RedirectResponse("/teams", status_code=303)
    form = await request.form()
    team.name = str(form.get("name") or team.name).strip()
    team.description = str(form.get("description") or "")
    team.rpm_limit = int(form["rpm_limit"]) if form.get("rpm_limit") else None
    team.concurrency_limit = (
        int(form["concurrency_limit"]) if form.get("concurrency_limit") else None
    )
    team.daily_quota = int(form["daily_quota"]) if form.get("daily_quota") else None
    team.monthly_quota = int(form["monthly_quota"]) if form.get("monthly_quota") else None
    team.priority = int(form.get("priority") or 0)
    names = source_names(db)
    services = _parse_services(form.getlist("services"), names)
    models = _collect_models_from_form(form, db)
    _sync_team_grants(db, team, services)
    _sync_team_models(db, team, models)
    sync_team_model_limits(db, team, str(form.get("model_limits") or ""))
    _sync_team_members(db, team, form.getlist("members"), form.getlist("owners"))
    _apply_team_routing_from_form(team, form, db)
    write_audit(
        db, actor=user, action="team.update", entity_type="team", entity_id=team.id, detail=team.name
    )
    db.commit()
    return RedirectResponse("/teams", status_code=303)


@router.post("/teams/{team_id}/delete")
def teams_delete(
    team_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_platform_admin)],
):
    _require_teams_feature(db)
    team = db.get(Team, team_id)
    if team:
        write_audit(
            db, actor=user, action="team.delete", entity_type="team", entity_id=team.id, detail=team.name
        )
        db.delete(team)
        db.commit()
    return RedirectResponse("/teams", status_code=303)
