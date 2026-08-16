from __future__ import annotations

import csv
import io
from datetime import timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from ..audit import write_audit
from ..config import API_STYLES, KINDS, MODEL_CHECK_KINDS, Settings, dialect_choices
from ..data.backends import get_source_by_name, list_sources, source_names
from ..data.catalog import list_catalog
from ..data.dialects import dialect_blurb_for_kind
from ..data.db import (
    generate_api_key,
    get_db,
    hash_api_key,
    hash_password,
    key_display_prefix,
    verify_password,
)
from ..data.models import (
    AdminUser,
    ApiKey,
    CatalogModel,
    ModelAllowlist,
    ModelFavorite,
    ServiceGrant,
    Team,
    TeamMember,
    UsageEvent,
    utcnow,
)
from .access import (
    Forbidden,
    RedirectToLogin,
    can_access_key,
    can_access_team,
    current_user,
    require_platform_admin,
    require_user,
    scope_keys_query,
    user_team_ids,
    user_teams,
)
from .ops import sync_key_model_limits, sync_team_model_limits, _format_model_limits

from .templating import make_templates

templates = make_templates()
router = APIRouter()


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _parse_services(form_list: list[str] | None, allowed: set[str] | list[str]) -> list[str]:
    allowed_set = set(allowed)
    if not form_list:
        return []
    return [s for s in form_list if s in allowed_set]


def _parse_models(raw: str, default_service: str, db: Session) -> list[tuple[str, str]]:
    """Parse lines like 'chat:llama3' or bare names for a given default source."""
    by_name = {s.name: s for s in list_sources(db)}
    out: list[tuple[str, str]] = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if ":" in line:
            svc, name = line.split(":", 1)
            svc, name = svc.strip(), name.strip()
            src = by_name.get(svc)
            if src and src.kind in MODEL_CHECK_KINDS and name:
                out.append((svc, name))
        else:
            src = by_name.get(default_service)
            if src and src.kind in MODEL_CHECK_KINDS:
                out.append((default_service, line))
    return out


def _parse_model_checks(form_list: list[str] | None) -> list[tuple[str, str]]:
    """Parse checkbox values 'source:model'."""
    out: list[tuple[str, str]] = []
    for raw in form_list or []:
        raw = (raw or "").strip()
        if ":" not in raw:
            continue
        svc, name = raw.split(":", 1)
        svc, name = svc.strip(), name.strip()
        if svc and name:
            out.append((svc, name))
    return out


def _collect_models_from_form(form, db: Session) -> list[tuple[str, str]]:
    """Checkbox allowlist from catalog. Empty = unrestricted."""
    _ = db
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str]] = []
    for pair in _parse_model_checks(form.getlist("models")):
        if pair not in seen:
            seen.add(pair)
            out.append(pair)
    return out


def _collect_favorites_from_form(form, db: Session) -> list[tuple[str, str, int]]:
    """Pinned models with order = checkbox list index."""
    _ = db
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, int]] = []
    for pair in _parse_model_checks(form.getlist("favorites")):
        if pair in seen:
            continue
        seen.add(pair)
        out.append((pair[0], pair[1], len(out)))
    return out


def _selected_model_keys(rows: list[ModelAllowlist] | list[ModelFavorite]) -> set[str]:
    return {f"{m.service}:{m.model_name}" for m in rows}


def _catalog_for_allowlist(
    db: Session, selected: set[str]
) -> list[tuple[str, list[CatalogModel]]]:
    """Enabled catalog rows grouped by source; orphans from allowlist appended."""
    by_source: dict[str, list[CatalogModel]] = {}
    seen: set[str] = set()
    for row in list_catalog(db):
        if not row.enabled and f"{row.source_name}:{row.model_id}" not in selected:
            continue
        by_source.setdefault(row.source_name, []).append(row)
        seen.add(f"{row.source_name}:{row.model_id}")
    for key in sorted(selected - seen):
        if ":" not in key:
            continue
        svc, mid = key.split(":", 1)
        by_source.setdefault(svc, []).append(
            CatalogModel(source_name=svc, kind="chat", model_id=mid, enabled=True)
        )
    return sorted(by_source.items(), key=lambda x: x[0])


def _sync_key_grants(db: Session, api_key: ApiKey, services: list[str]) -> None:
    db.query(ServiceGrant).filter(ServiceGrant.api_key_id == api_key.id).delete()
    for s in services:
        db.add(ServiceGrant(api_key_id=api_key.id, service=s))


def _sync_key_models(db: Session, api_key: ApiKey, models: list[tuple[str, str]]) -> None:
    db.query(ModelAllowlist).filter(ModelAllowlist.api_key_id == api_key.id).delete()
    for svc, name in models:
        db.add(ModelAllowlist(api_key_id=api_key.id, service=svc, model_name=name))


def _sync_key_favorites(
    db: Session, api_key: ApiKey, favorites: list[tuple[str, str, int]]
) -> None:
    db.query(ModelFavorite).filter(ModelFavorite.api_key_id == api_key.id).delete()
    for svc, name, order in favorites:
        db.add(
            ModelFavorite(
                api_key_id=api_key.id,
                service=svc,
                model_name=name,
                sort_order=order,
            )
        )


def _sync_team_grants(db: Session, team: Team, services: list[str]) -> None:
    db.query(ServiceGrant).filter(ServiceGrant.team_id == team.id).delete()
    for s in services:
        db.add(ServiceGrant(team_id=team.id, service=s))


def _sync_team_models(db: Session, team: Team, models: list[tuple[str, str]]) -> None:
    db.query(ModelAllowlist).filter(ModelAllowlist.team_id == team.id).delete()
    for svc, name in models:
        db.add(ModelAllowlist(team_id=team.id, service=svc, model_name=name))


def _sync_team_favorites(
    db: Session, team: Team, favorites: list[tuple[str, str, int]]
) -> None:
    db.query(ModelFavorite).filter(ModelFavorite.team_id == team.id).delete()
    for svc, name, order in favorites:
        db.add(
            ModelFavorite(
                team_id=team.id,
                service=svc,
                model_name=name,
                sort_order=order,
            )
        )


def _resolve_ceiling(
    db: Session,
    *,
    teams_on: bool,
    acting_user: AdminUser,
    team_id: int | None = None,
    owner_user_id: int | None = None,
    api_key: ApiKey | None = None,
):
    from ..data.grants import (
        ceiling_for_key,
        ceiling_from_team,
        ceiling_from_user,
        load_user_with_grants,
    )

    if api_key is not None:
        return ceiling_for_key(db, api_key)
    if teams_on:
        if team_id:
            team = (
                db.query(Team)
                .options(
                    joinedload(Team.service_grants),
                    joinedload(Team.model_allowlists),
                )
                .filter(Team.id == team_id)
                .first()
            )
            if team:
                return ceiling_from_team(team)
        from ..data.grants import AccessCeiling

        return AccessCeiling(unrestricted=False, services=set(), label="no-team")
    uid = owner_user_id or acting_user.id
    owner = load_user_with_grants(db, uid) or acting_user
    return ceiling_from_user(owner)


def _key_form_context(
    db: Session,
    *,
    user: AdminUser,
    teams_on: bool,
    api_key: ApiKey | None,
    teams: list,
    owners: list,
    selected_services: list[str] | None = None,
    team_id: int | None = None,
    owner_user_id: int | None = None,
) -> dict:
    from ..data.grants import (
        catalog_groups_for_ceiling,
        grant_summary,
        services_for_ceiling,
    )

    if api_key is not None:
        team_id = api_key.team_id
        owner_user_id = api_key.owner_user_id
    ceil = _resolve_ceiling(
        db,
        teams_on=teams_on,
        acting_user=user,
        team_id=team_id,
        owner_user_id=owner_user_id,
        api_key=api_key,
    )
    services = services_for_ceiling(db, ceil)
    if selected_services is None:
        if api_key is not None:
            selected_services = [g.service for g in api_key.service_grants]
        else:
            selected_services = []  # empty = inherit grant
    selected_models = (
        _selected_model_keys(list(api_key.model_allowlists)) if api_key else set()
    )
    selected_favorites = (
        _selected_model_keys(list(api_key.model_favorites)) if api_key else set()
    )
    return {
        "user": user,
        "key": api_key,
        "services": services,
        "selected_services": selected_services,
        "teams": teams,
        "owners": owners,
        "catalog_groups": catalog_groups_for_ceiling(db, ceil),
        "selected_models": selected_models,
        "selected_favorites": selected_favorites,
        "model_limits_text": (
            _format_model_limits(api_key.model_limits) if api_key else ""
        ),
        "nav": "keys",
        "is_admin": user.is_platform_admin,
        "teams_enabled": teams_on,
        "ceiling": ceil,
        "grant_summary": grant_summary(ceil),
        "grant_empty": (not ceil.unrestricted and not ceil.services),
    }


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    from .accounts import get_auth_settings

    reset_ok = request.query_params.get("reset") == "1"
    auth = get_auth_settings(db)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "error": None,
            "reset_ok": reset_ok,
            "allow_register": auth.allow_self_registration,
        },
    )


@router.post("/login")
def login_submit(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: str = Form(...),
    password: str = Form(...),
):
    from .accounts import find_user_by_login, get_auth_settings

    user = find_user_by_login(db, username)
    if user is None or not user.is_active or not verify_password(password, user.password_hash):
        auth = get_auth_settings(db)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "error": "Invalid username/email or password",
                "reset_ok": False,
                "allow_register": auth.allow_self_registration,
            },
            status_code=401,
        )
    request.session["user_id"] = user.id
    if user.must_change_password:
        return RedirectResponse("/account", status_code=303)
    if user.is_platform_admin:
        from .setup import needs_setup_wizard, wizard_progress

        if needs_setup_wizard(db, user):
            nxt = wizard_progress(db)["next"]
            return RedirectResponse(
                (nxt["path"] if nxt else "/setup"), status_code=303
            )
        return RedirectResponse("/", status_code=303)
    return RedirectResponse("/me", status_code=303)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _wizard_or_next(db: Session, user: AdminUser):
    from .setup import needs_setup_wizard, wizard_progress

    if not needs_setup_wizard(db, user):
        return None
    nxt = wizard_progress(db)["next"]
    return RedirectResponse((nxt["path"] if nxt else "/setup/done"), status_code=303)


# ---- First-run setup wizard ----


@router.get("/setup", response_class=HTMLResponse)
def setup_root(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from .setup import wizard_progress

    wiz = wizard_progress(db)
    if wiz["complete"]:
        return RedirectResponse("/setup/done", status_code=303)
    return RedirectResponse(wiz["next"]["path"], status_code=303)


@router.get("/setup/sources", response_class=HTMLResponse)
def setup_sources_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..config import KINDS
    from .setup import wizard_progress

    wiz = wizard_progress(db)
    return templates.TemplateResponse(
        request,
        "setup_sources.html",
        {
            "user": user,
            "nav": "setup",
            "wizard": wiz,
            "step_id": "sources",
            "step_title": "Step 1 · Backends",
            "step_lede": "Point the gateway at your local chat / embed / STT / TTS servers.",
            "kinds": KINDS,
            "flash_ok": request.session.pop("flash_ok", None),
            "flash_err": request.session.pop("flash_err", None),
            "is_admin": True,
        },
    )


@router.post("/setup/sources")
def setup_sources_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    name: str = Form(""),
    kind: str = Form("chat"),
    address: str = Form(""),
):
    from ..data.backends import (
        upsert_source,
        validate_backend,
        validate_kind,
        validate_source_name,
    )
    from ..data.models import BackendSource

    err = validate_source_name(name) or validate_kind(kind) or validate_backend(address)
    if err:
        request.session["flash_err"] = err
        return RedirectResponse("/setup/sources", status_code=303)
    existing = get_source_by_name(db, name.strip().lower())
    if existing:
        request.session["flash_err"] = f"Source '{name}' already exists — change the name or edit under Services."
        return RedirectResponse("/setup/sources", status_code=303)

    # First source of this kind becomes the /v1/… default; extras stay non-default
    # (reachable via /s/{name}/…). No checkbox in the wizard.
    has_kind = (
        db.query(BackendSource)
        .filter(BackendSource.kind == kind)
        .count()
        > 0
    )
    src = upsert_source(
        db,
        name=name,
        kind=kind,
        address=address,
        is_default=not has_kind,
        route_models="",
        isolated=False,
        api_style="auto",
    )
    write_audit(
        db,
        actor=user,
        action="setup.source",
        entity_type="backend_source",
        entity_id=src.id,
        detail=f"{src.name}@{src.address}",
    )
    db.commit()
    request.session["flash_ok"] = f"Added {src.name} → {src.address}. Add more or continue."
    return RedirectResponse("/setup/sources", status_code=303)


@router.get("/setup/models", response_class=HTMLResponse)
def setup_models_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from .setup import wizard_progress

    wiz = wizard_progress(db)
    if not wiz["has_sources"]:
        return RedirectResponse("/setup/sources", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup_models.html",
        {
            "user": user,
            "nav": "setup",
            "wizard": wiz,
            "step_id": "models",
            "step_title": "Step 2 · Sync models",
            "step_lede": "Discover models from each backend into the catalog.",
            "flash_ok": request.session.pop("flash_ok", None),
            "flash_err": request.session.pop("flash_err", None),
            "is_admin": True,
        },
    )


@router.post("/setup/models")
def setup_models_sync(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.catalog import sync_catalog_from_sources
    from .setup import wizard_progress

    if not wizard_progress(db)["has_sources"]:
        return RedirectResponse("/setup/sources", status_code=303)
    stats = sync_catalog_from_sources(db)
    write_audit(
        db,
        actor=user,
        action="setup.catalog.sync",
        entity_type="catalog_models",
        detail=str(stats),
    )
    db.commit()
    if stats.get("seen", 0) == 0:
        request.session["flash_err"] = (
            "Sync found 0 models — check source addresses are reachable, then try again."
        )
        return RedirectResponse("/setup/models", status_code=303)
    request.session["flash_ok"] = (
        f"Synced {stats['seen']} models ({stats.get('created', 0)} new)."
    )
    return RedirectResponse("/setup/key", status_code=303)


@router.get("/setup/key", response_class=HTMLResponse)
def setup_key_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    import os

    from ..data.grants import AccessCeiling, catalog_groups_for_ceiling
    from ..vision_route import group_models_vl_pairs
    from .accounts import get_auth_settings
    from .setup import wizard_progress

    wiz = wizard_progress(db)
    if not wiz["has_sources"]:
        return RedirectResponse("/setup/sources", status_code=303)
    if not wiz["has_models"]:
        return RedirectResponse("/setup/models", status_code=303)
    created = request.session.pop("flash_key", None)
    created_summary = request.session.pop("flash_key_services", None)
    created_models_n = request.session.pop("flash_key_models_n", None)
    ceil = AccessCeiling(unrestricted=True, label="setup")
    auth = get_auth_settings(db)
    catalog_groups = catalog_groups_for_ceiling(db, ceil)
    catalog_vl_groups = [
        (src, group_models_vl_pairs(models)) for src, models in catalog_groups
    ]
    return templates.TemplateResponse(
        request,
        "setup_key.html",
        {
            "user": user,
            "nav": "setup",
            "wizard": wiz,
            "step_id": "key",
            "step_title": "Step 3 · Your API key",
            "step_lede": "Choose which sources (and optionally models) this key may use.",
            "created_key": created,
            "created_summary": created_summary,
            "created_models_n": created_models_n,
            "sources": wiz["sources"],
            "catalog_groups": catalog_groups,
            "catalog_vl_groups": catalog_vl_groups,
            "auto_vl_routing": bool(auth.auto_vl_routing),
            "gateway_port": os.getenv("GATEWAY_PORT", "9081"),
            "flash_ok": request.session.pop("flash_ok", None),
            "flash_err": request.session.pop("flash_err", None),
            "is_admin": True,
        },
    )


@router.post("/setup/key")
async def setup_key_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.grants import AccessCeiling, clamp_models, clamp_services
    from .accounts import get_auth_settings
    from .setup import wizard_progress

    wiz = wizard_progress(db)
    if not wiz["has_sources"]:
        return RedirectResponse("/setup/sources", status_code=303)
    if not wiz["has_models"]:
        return RedirectResponse("/setup/models", status_code=303)

    form = await request.form()
    label = str(form.get("label") or "").strip() or "main"
    names = source_names(db)
    ceil = AccessCeiling(unrestricted=True, label="setup")
    services = clamp_services(
        _parse_services(form.getlist("services"), names), ceil, db
    )
    if not services:
        request.session["flash_err"] = "Check at least one source for this key."
        return RedirectResponse("/setup/key", status_code=303)

    models = clamp_models(_collect_models_from_form(form, db), ceil)
    # Only keep model rows for selected sources
    models = [(s, m) for s, m in models if s in services]

    auth = get_auth_settings(db)
    auth.auto_vl_routing = str(form.get("auto_vl_routing") or "") == "on"

    raw = generate_api_key()
    api_key = ApiKey(
        label=label,
        key_hash=hash_api_key(raw),
        key_prefix=key_display_prefix(raw),
        owner_user_id=user.id,
        is_active=True,
        priority=0,
    )
    db.add(api_key)
    db.flush()
    _sync_key_grants(db, api_key, services)
    _sync_key_models(db, api_key, models)
    write_audit(
        db,
        actor=user,
        action="setup.key",
        entity_type="api_key",
        entity_id=api_key.id,
        detail=(
            f"{api_key.label} services={services} models={len(models)} "
            f"auto_vl={auth.auto_vl_routing}"
        ),
    )
    db.commit()
    request.session["flash_key"] = raw
    request.session["flash_key_services"] = ", ".join(services)
    request.session["flash_key_models_n"] = len(models) if models else 0
    return RedirectResponse("/setup/key", status_code=303)


@router.get("/setup/done", response_class=HTMLResponse)
def setup_done_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from .setup import wizard_progress

    if not wizard_progress(db)["complete"]:
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request,
        "setup_done.html",
        {"user": user, "nav": "setup", "is_admin": True},
    )


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    if not user.is_platform_admin:
        return RedirectResponse("/me", status_code=303)
    redir = _wizard_or_next(db, user)
    if redir is not None:
        return redir

    from ..stats import (
        bar_chart_svg,
        daily_traffic_chart_svg,
        usage_stats,
        week_window_start,
        zone_from_request,
    )
    from .setup import setup_status

    zone = zone_from_request(request, user)
    now = utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = week_window_start(zone)
    day = usage_stats(db, since=day_ago, tz=zone)
    week = usage_stats(db, since=week_ago, tz=zone)
    tz_label = str(zone)
    active_keys = db.query(func.count(ApiKey.id)).filter(ApiKey.is_active.is_(True)).scalar() or 0
    teams_on = _teams_on(db)
    teams_count = (
        (db.query(func.count(Team.id)).scalar() or 0) if teams_on else 0
    )
    flash_ok = request.session.pop("flash_ok", None)
    setup = setup_status(db)
    demo_tools = bool(getattr(_settings(request), "demo_tools", False))

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "user": user,
            "total_day": day["total_ok"],
            "total_week": week["total_ok"],
            "denies": day["denies"],
            "rate_limits": day["rate_limits"],
            "top_keys": day["top_keys"],
            "by_service": day["by_service"],
            "by_model": day["by_model"],
            "tokens_in": day["tokens_in"],
            "tokens_out": day["tokens_out"],
            "audio_seconds": day["audio_seconds"],
            "response_chars": day["response_chars"],
            "latency_p50": day["latency_p50"],
            "latency_p95": day["latency_p95"],
            "latency_avg": day["latency_avg"],
            "demo_count": week["demo_count"] if demo_tools else 0,
            "daily_series": week["daily_series"],
            "chart_service": bar_chart_svg(day["by_service"], unit="requests"),
            "chart_model": bar_chart_svg([(m, c) for m, c in day["by_model"]], unit="requests"),
            "chart_keys": bar_chart_svg(day["top_keys"], unit="requests"),
            "chart_daily": daily_traffic_chart_svg(week["daily_series"], tz_label=tz_label),
            "active_keys": active_keys,
            "teams_count": teams_count,
            "teams_enabled": teams_on,
            "demo_tools": demo_tools,
            "display_timezone": tz_label,
            "flash_ok": flash_ok,
            "nav": "dashboard",
            "is_admin": True,
            "setup": setup,
        },
    )


@router.get("/me", response_class=HTMLResponse)
def me_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    from ..data.models import UsageDaily
    from ..stats import (
        bar_chart_svg,
        daily_traffic_chart_svg,
        usage_stats,
        week_window_start,
        zone_from_request,
    )

    teams_on = _teams_on(db)
    keys_q = scope_keys_query(
        db.query(ApiKey).options(
            joinedload(ApiKey.team),
            joinedload(ApiKey.model_favorites),
        ),
        user,
        teams_enabled=teams_on,
    )
    keys = keys_q.order_by(ApiKey.label).all()
    key_ids = [k.id for k in keys]
    zone = zone_from_request(request, user)
    now = utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = week_window_start(zone)
    day = usage_stats(db, since=day_ago, key_ids=key_ids, tz=zone)
    week = usage_stats(db, since=week_ago, key_ids=key_ids, tz=zone)
    today = now.astimezone(zone).date()
    tz_label = str(zone)
    daily_q = db.query(UsageDaily).filter(UsageDaily.day == today)
    if key_ids:
        daily_q = daily_q.filter(UsageDaily.api_key_id.in_(key_ids))
    else:
        daily_q = daily_q.filter(False)
    daily_rows = daily_q.order_by(UsageDaily.ok_count.desc()).limit(50).all()
    fav_key_id = request.query_params.get("fav_key")
    fav_key = None
    if fav_key_id:
        try:
            kid = int(fav_key_id)
        except ValueError:
            kid = None
        if kid is not None:
            fav_key = next((k for k in keys if k.id == kid), None)
    if fav_key is None and keys:
        fav_key = keys[0]
    selected_favs = (
        _selected_model_keys(fav_key.model_favorites) if fav_key else set()
    )
    catalog_groups = (
        _catalog_for_allowlist(db, selected_favs) if fav_key else []
    )
    flash_ok = request.session.pop("flash_ok", None)
    return templates.TemplateResponse(
        request,
        "me.html",
        {
            "user": user,
            "ok": day["total_ok"],
            "deny": day["denies"],
            "rate": day["rate_limits"],
            "tokens_in": day["tokens_in"],
            "tokens_out": day["tokens_out"],
            "audio_seconds": int(day["audio_seconds"] or 0),
            "latency_p50": day["latency_p50"],
            "latency_p95": day["latency_p95"],
            "chart_daily": daily_traffic_chart_svg(week["daily_series"], tz_label=tz_label),
            "chart_service": bar_chart_svg(day["by_service"], unit="requests"),
            "daily_rows": daily_rows,
            "keys": keys,
            "teams": user_teams(user) if teams_on else [],
            "fav_key": fav_key,
            "catalog_groups": catalog_groups,
            "selected_favorites": selected_favs,
            "flash_ok": flash_ok,
            "display_timezone": tz_label,
            "nav": "me",
            "is_admin": user.is_platform_admin,
            "teams_enabled": teams_on,
        },
    )


@router.post("/me/favorites")
async def me_favorites_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    form = await request.form()
    try:
        key_id = int(form.get("key_id") or 0)
    except (TypeError, ValueError):
        return RedirectResponse("/me", status_code=303)
    api_key = db.get(ApiKey, key_id)
    if api_key is None or not can_access_key(user, api_key, teams_enabled=teams_on):
        raise Forbidden()
    favorites = _collect_favorites_from_form(form, db)
    _sync_key_favorites(db, api_key, favorites)
    write_audit(
        db,
        actor=user,
        action="key.favorites",
        entity_type="api_key",
        entity_id=api_key.id,
        detail=f"n={len(favorites)}",
    )
    db.commit()
    request.session["flash_ok"] = f"Favorites saved for {api_key.label}."
    return RedirectResponse(f"/me?fav_key={api_key.id}", status_code=303)


@router.post("/demo/seed")
def demo_seed(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    if not _settings(request).demo_tools:
        raise Forbidden()
    from ..demo_seed import seed_demo_usage

    n = seed_demo_usage(db, count=120)
    write_audit(db, actor=user, action="demo.seed", detail=f"events={n}")
    db.commit()
    request.session["flash_ok"] = f"Seeded {n} demo usage events (tagged is_demo)."
    return RedirectResponse("/", status_code=303)


@router.post("/demo/clear")
def demo_clear(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    if not _settings(request).demo_tools:
        raise Forbidden()
    from ..demo_seed import clear_demo_usage

    n = clear_demo_usage(db)
    write_audit(db, actor=user, action="demo.clear", detail=f"removed={n}")
    db.commit()
    request.session["flash_ok"] = f"Removed {n} demo usage events."
    return RedirectResponse("/", status_code=303)


# ---- Keys ----


def _teams_on(db: Session) -> bool:
    from .accounts import teams_feature_enabled

    return teams_feature_enabled(db)


@router.get("/keys", response_class=HTMLResponse)
def keys_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    q = db.query(ApiKey).options(joinedload(ApiKey.team), joinedload(ApiKey.service_grants))
    q = scope_keys_query(q, user, teams_enabled=teams_on)
    keys = q.order_by(ApiKey.created_at.desc()).all()
    teams = (
        (
            db.query(Team).order_by(Team.name).all()
            if user.is_platform_admin
            else user_teams(user)
        )
        if teams_on
        else []
    )
    return templates.TemplateResponse(
        request,
        "keys.html",
        {
            "user": user,
            "keys": keys,
            "services": source_names(db),
            "teams": teams,
            "flash_key": request.session.pop("flash_key", None),
            "nav": "keys",
            "is_admin": user.is_platform_admin,
            "teams_enabled": teams_on,
        },
    )


@router.get("/keys/new", response_class=HTMLResponse)
def keys_new(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    teams = (
        (
            db.query(Team).order_by(Team.name).all()
            if user.is_platform_admin
            else user_teams(user)
        )
        if teams_on
        else []
    )
    owners = (
        db.query(AdminUser).order_by(AdminUser.username).all()
        if user.is_platform_admin and not teams_on
        else []
    )
    default_team = teams[0].id if teams_on and len(teams) == 1 else None
    ctx = _key_form_context(
        db,
        user=user,
        teams_on=teams_on,
        api_key=None,
        teams=teams,
        owners=owners,
        team_id=default_team,
        owner_user_id=user.id,
    )
    return templates.TemplateResponse(request, "key_form.html", ctx)


@router.post("/keys/new")
async def keys_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    from ..data.grants import clamp_models, clamp_services

    teams_on = _teams_on(db)
    form = await request.form()
    label = str(form.get("label") or "").strip() or "unnamed"
    team_id = None
    owner_user_id = user.id
    if teams_on:
        team_id_raw = form.get("team_id")
        team_id = int(team_id_raw) if team_id_raw else None
        if not user.is_platform_admin:
            if team_id is None or not can_access_team(user, team_id):
                raise Forbidden()
        owner_user_id = user.id
    else:
        if user.is_platform_admin and form.get("owner_user_id"):
            owner_user_id = int(form.get("owner_user_id"))
        else:
            owner_user_id = user.id
        team_id = None

    ceil = _resolve_ceiling(
        db,
        teams_on=teams_on,
        acting_user=user,
        team_id=team_id,
        owner_user_id=owner_user_id,
    )
    names = source_names(db)
    services = clamp_services(
        _parse_services(form.getlist("services"), names), ceil, db
    )
    models = clamp_models(_collect_models_from_form(form, db), ceil)
    favorites = clamp_models(
        [(s, m) for s, m, _ in _collect_favorites_from_form(form, db)], ceil
    )
    fav_rows = [(s, m, i) for i, (s, m) in enumerate(favorites)]
    rpm = form.get("rpm_limit")
    concurrency = form.get("concurrency_limit")
    priority = form.get("priority")
    daily = form.get("daily_quota")

    raw = generate_api_key()
    api_key = ApiKey(
        label=label,
        key_hash=hash_api_key(raw),
        key_prefix=key_display_prefix(raw),
        team_id=team_id,
        owner_user_id=owner_user_id,
        is_active=True,
        rpm_limit=int(rpm) if rpm else None,
        concurrency_limit=int(concurrency) if concurrency else None,
        daily_quota=int(daily) if daily else None,
        priority=int(priority) if priority not in (None, "") else None,
    )
    db.add(api_key)
    db.flush()
    _sync_key_grants(db, api_key, services)
    _sync_key_models(db, api_key, models)
    _sync_key_favorites(db, api_key, fav_rows)
    sync_key_model_limits(db, api_key, str(form.get("model_limits") or ""))
    write_audit(
        db,
        actor=user,
        action="key.create",
        entity_type="api_key",
        entity_id=api_key.id,
        detail=f"label={label} team_id={team_id} owner={owner_user_id}",
    )
    db.commit()
    request.session["flash_key"] = raw
    return RedirectResponse("/keys", status_code=303)


@router.get("/keys/{key_id}", response_class=HTMLResponse)
def keys_edit(
    key_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    api_key = (
        db.query(ApiKey)
        .options(
            joinedload(ApiKey.service_grants),
            joinedload(ApiKey.model_allowlists),
            joinedload(ApiKey.model_favorites),
            joinedload(ApiKey.model_limits),
            joinedload(ApiKey.team).joinedload(Team.service_grants),
            joinedload(ApiKey.team).joinedload(Team.model_allowlists),
            joinedload(ApiKey.owner).joinedload(AdminUser.service_grants),
            joinedload(ApiKey.owner).joinedload(AdminUser.model_allowlists),
        )
        .filter(ApiKey.id == key_id)
        .first()
    )
    if not api_key:
        return RedirectResponse("/keys", status_code=303)
    if not can_access_key(user, api_key, teams_enabled=teams_on):
        raise Forbidden()
    teams = (
        (
            db.query(Team).order_by(Team.name).all()
            if user.is_platform_admin
            else user_teams(user)
        )
        if teams_on
        else []
    )
    owners = (
        db.query(AdminUser).order_by(AdminUser.username).all()
        if user.is_platform_admin and not teams_on
        else []
    )
    ctx = _key_form_context(
        db,
        user=user,
        teams_on=teams_on,
        api_key=api_key,
        teams=teams,
        owners=owners,
    )
    return templates.TemplateResponse(request, "key_form.html", ctx)


@router.post("/keys/{key_id}")
async def keys_update(
    key_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    from ..data.grants import clamp_models, clamp_services

    teams_on = _teams_on(db)
    api_key = db.get(ApiKey, key_id)
    if not api_key:
        return RedirectResponse("/keys", status_code=303)
    if not can_access_key(user, api_key, teams_enabled=teams_on):
        raise Forbidden()
    form = await request.form()
    api_key.label = str(form.get("label") or "").strip() or api_key.label
    if teams_on:
        team_id = form.get("team_id")
        new_team = int(team_id) if team_id else None
        if not user.is_platform_admin and new_team and not can_access_team(user, new_team):
            raise Forbidden()
        api_key.team_id = new_team
    else:
        api_key.team_id = None
        if user.is_platform_admin and form.get("owner_user_id"):
            api_key.owner_user_id = int(form.get("owner_user_id"))
        elif not api_key.owner_user_id:
            api_key.owner_user_id = user.id
    api_key.is_active = form.get("is_active") == "on"
    rpm = form.get("rpm_limit")
    concurrency = form.get("concurrency_limit")
    priority = form.get("priority")
    daily = form.get("daily_quota")
    api_key.rpm_limit = int(rpm) if rpm else None
    api_key.concurrency_limit = int(concurrency) if concurrency else None
    api_key.priority = int(priority) if priority not in (None, "") else None
    api_key.daily_quota = int(daily) if daily else None

    ceil = _resolve_ceiling(
        db,
        teams_on=teams_on,
        acting_user=user,
        team_id=api_key.team_id,
        owner_user_id=api_key.owner_user_id,
        api_key=api_key,
    )
    # Refresh team/owner relationships for ceiling after team change
    if api_key.team_id:
        db.refresh(api_key)
        team = (
            db.query(Team)
            .options(
                joinedload(Team.service_grants),
                joinedload(Team.model_allowlists),
            )
            .filter(Team.id == api_key.team_id)
            .first()
        )
        api_key.team = team
        from ..data.grants import ceiling_from_team

        ceil = ceiling_from_team(team) if team else ceil
    else:
        from ..data.grants import ceiling_from_user, load_user_with_grants

        owner = load_user_with_grants(db, api_key.owner_user_id)
        if owner:
            ceil = ceiling_from_user(owner)

    names = source_names(db)
    services = clamp_services(
        _parse_services(form.getlist("services"), names), ceil, db
    )
    models = clamp_models(_collect_models_from_form(form, db), ceil)
    favorites = clamp_models(
        [(s, m) for s, m, _ in _collect_favorites_from_form(form, db)], ceil
    )
    fav_rows = [(s, m, i) for i, (s, m) in enumerate(favorites)]
    _sync_key_grants(db, api_key, services)
    _sync_key_models(db, api_key, models)
    _sync_key_favorites(db, api_key, fav_rows)
    sync_key_model_limits(db, api_key, str(form.get("model_limits") or ""))
    write_audit(
        db,
        actor=user,
        action="key.update",
        entity_type="api_key",
        entity_id=api_key.id,
        detail=api_key.label,
    )
    db.commit()
    return RedirectResponse("/keys", status_code=303)


@router.post("/keys/{key_id}/rotate")
def keys_rotate(
    key_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    api_key = db.get(ApiKey, key_id)
    if not api_key:
        return RedirectResponse("/keys", status_code=303)
    if not can_access_key(user, api_key, teams_enabled=teams_on):
        raise Forbidden()
    raw = generate_api_key()
    api_key.key_hash = hash_api_key(raw)
    api_key.key_prefix = key_display_prefix(raw)
    write_audit(
        db, actor=user, action="key.rotate", entity_type="api_key", entity_id=api_key.id
    )
    db.commit()
    request.session["flash_key"] = raw
    return RedirectResponse("/keys", status_code=303)


@router.post("/keys/{key_id}/revoke")
def keys_revoke(
    key_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    teams_on = _teams_on(db)
    api_key = db.get(ApiKey, key_id)
    if api_key:
        if not can_access_key(user, api_key, teams_enabled=teams_on):
            raise Forbidden()
        api_key.is_active = False
        write_audit(
            db, actor=user, action="key.revoke", entity_type="api_key", entity_id=api_key.id
        )
        db.commit()
    return RedirectResponse("/keys", status_code=303)


# ---- Teams ----


def _require_teams_feature(db: Session) -> None:
    if not _teams_on(db):
        raise Forbidden()


@router.get("/teams", response_class=HTMLResponse)
def teams_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
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
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.grants import AccessCeiling, catalog_groups_for_ceiling

    _require_teams_feature(db)
    names = source_names(db)
    return templates.TemplateResponse(
        request,
        "team_form.html",
        {
            "user": user,
            "team": None,
            "services": names,
            "selected_services": [],
            "catalog_groups": catalog_groups_for_ceiling(
                db, AccessCeiling(unrestricted=True)
            ),
            "selected_models": set(),
            "selected_favorites": set(),
            "model_limits_text": "",
            "users": db.query(AdminUser).order_by(AdminUser.username).all(),
            "member_ids": [],
            "nav": "teams",
            "is_admin": True,
            "read_only": False,
        },
    )


@router.post("/teams/new")
async def teams_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
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
    favorites = _collect_favorites_from_form(form, db)
    _sync_team_grants(db, team, services)
    _sync_team_models(db, team, models)
    _sync_team_favorites(db, team, favorites)
    sync_team_model_limits(db, team, str(form.get("model_limits") or ""))
    for uid in form.getlist("members"):
        db.add(TeamMember(team_id=team.id, user_id=int(uid), role="member"))
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
    user: Annotated[AdminUser, Depends(require_user)],
):
    _require_teams_feature(db)
    if not can_access_team(user, team_id) and not user.is_platform_admin:
        raise Forbidden()
    team = (
        db.query(Team)
        .options(
            joinedload(Team.service_grants),
            joinedload(Team.model_allowlists),
            joinedload(Team.model_favorites),
            joinedload(Team.model_limits),
            joinedload(Team.members).joinedload(TeamMember.user),
        )
        .filter(Team.id == team_id)
        .first()
    )
    if not team:
        return RedirectResponse("/teams", status_code=303)
    selected = _selected_model_keys(team.model_allowlists)
    selected_favs = _selected_model_keys(team.model_favorites)
    # Only platform admins see the full user directory for membership editing.
    if user.is_platform_admin:
        users = db.query(AdminUser).order_by(AdminUser.username).all()
    else:
        users = [m.user for m in team.members if m.user]
    from ..data.grants import AccessCeiling, catalog_groups_for_ceiling

    return templates.TemplateResponse(
        request,
        "team_form.html",
        {
            "user": user,
            "team": team,
            "services": source_names(db),
            "selected_services": [g.service for g in team.service_grants],
            "catalog_groups": catalog_groups_for_ceiling(
                db, AccessCeiling(unrestricted=True)
            ),
            "selected_models": selected,
            "selected_favorites": selected_favs,
            "model_limits_text": _format_model_limits(team.model_limits),
            "users": users,
            "member_ids": [m.user_id for m in team.members],
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
    user: Annotated[AdminUser, Depends(require_user)],
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
    favorites = _collect_favorites_from_form(form, db)
    _sync_team_grants(db, team, services)
    _sync_team_models(db, team, models)
    _sync_team_favorites(db, team, favorites)
    sync_team_model_limits(db, team, str(form.get("model_limits") or ""))
    db.query(TeamMember).filter(TeamMember.team_id == team.id).delete()
    for uid in form.getlist("members"):
        db.add(TeamMember(team_id=team.id, user_id=int(uid), role="member"))
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
    user: Annotated[AdminUser, Depends(require_platform_admin)],
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


# ---- Users ----


@router.get("/users", response_class=HTMLResponse)
def users_list(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.grants import ceiling_from_user, grant_summary

    users = (
        db.query(AdminUser)
        .options(
            joinedload(AdminUser.service_grants),
            joinedload(AdminUser.model_allowlists),
        )
        .order_by(AdminUser.username)
        .all()
    )
    flash = request.session.pop("flash_ok", None)
    err = request.session.pop("flash_err", None)
    from ..mailer import smtp_ready, get_smtp

    grant_labels = {
        u.id: grant_summary(ceiling_from_user(u)) for u in users
    }
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "user": user,
            "users": users,
            "nav": "users",
            "is_admin": True,
            "flash_ok": flash,
            "flash_err": err,
            "smtp_ok": smtp_ready(get_smtp(db)),
            "teams_enabled": _teams_on(db),
            "grant_labels": grant_labels,
        },
    )


@router.get("/users/{user_id}/grant", response_class=HTMLResponse)
def users_grant_edit(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.grants import AccessCeiling, catalog_groups_for_ceiling, ceiling_from_user, grant_summary

    target = (
        db.query(AdminUser)
        .options(
            joinedload(AdminUser.service_grants),
            joinedload(AdminUser.model_allowlists),
        )
        .filter(AdminUser.id == user_id)
        .first()
    )
    if not target:
        return RedirectResponse("/users", status_code=303)
    if _teams_on(db):
        request.session["flash_err"] = (
            "Teams are enabled — set grants on the Team, not on individual users."
        )
        return RedirectResponse("/users", status_code=303)
    selected = _selected_model_keys(target.model_allowlists)
    return templates.TemplateResponse(
        request,
        "user_grant.html",
        {
            "user": user,
            "target": target,
            "nav": "users",
            "services": source_names(db),
            "selected_services": [g.service for g in target.service_grants],
            "catalog_groups": catalog_groups_for_ceiling(
                db, AccessCeiling(unrestricted=True)
            ),
            "selected_models": selected,
            "grant_summary": grant_summary(ceiling_from_user(target)),
            "flash_ok": request.session.pop("flash_ok", None),
            "flash_err": request.session.pop("flash_err", None),
        },
    )


@router.post("/users/{user_id}/grant")
async def users_grant_save(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.grants import sync_user_grants, sync_user_models

    if _teams_on(db):
        request.session["flash_err"] = "Teams are enabled — edit the Team grant instead."
        return RedirectResponse("/users", status_code=303)
    target = db.get(AdminUser, user_id)
    if not target:
        return RedirectResponse("/users", status_code=303)
    form = await request.form()
    names = source_names(db)
    services = _parse_services(form.getlist("services"), names)
    models = _collect_models_from_form(form, db)
    # Only keep models for granted sources
    models = [(s, m) for s, m in models if s in services]
    sync_user_grants(db, target, services)
    sync_user_models(db, target, models)
    write_audit(
        db,
        actor=user,
        action="user.grant",
        entity_type="user",
        entity_id=target.id,
        detail=f"services={services} models={len(models)}",
    )
    db.commit()
    request.session["flash_ok"] = f"Grant saved for {target.username}."
    return RedirectResponse(f"/users/{target.id}/grant", status_code=303)


@router.post("/users/new")
def users_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    username: str = Form(...),
    password: str = Form(...),
    email: str = Form(""),
    is_platform_admin: str = Form(""),
    must_change_password: str = Form(""),
):
    username = username.strip()
    email_n = email.strip().lower() or None
    if not username or not password:
        return RedirectResponse("/users", status_code=303)
    if db.query(AdminUser).filter(AdminUser.username == username).first():
        request.session["flash_err"] = "Username already exists."
        return RedirectResponse("/users", status_code=303)
    if email_n and db.query(AdminUser).filter(AdminUser.email == email_n).first():
        request.session["flash_err"] = "Email already exists."
        return RedirectResponse("/users", status_code=303)
    db.add(
        AdminUser(
            username=username,
            email=email_n,
            password_hash=hash_password(password),
            is_active=True,
            is_platform_admin=is_platform_admin == "on",
            must_change_password=must_change_password == "on",
        )
    )
    write_audit(
        db, actor=user, action="user.create", entity_type="user", detail=username
    )
    db.commit()
    request.session["flash_ok"] = f"User {username} created."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/toggle")
def users_toggle(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    target = db.get(AdminUser, user_id)
    if target and target.id != user.id:
        target.is_active = not target.is_active
        write_audit(
            db,
            actor=user,
            action="user.toggle",
            entity_type="user",
            entity_id=target.id,
            detail=f"active={target.is_active}",
        )
        db.commit()
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/update")
async def users_update(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    target = db.get(AdminUser, user_id)
    if not target:
        return RedirectResponse("/users", status_code=303)
    form = await request.form()
    email_n = str(form.get("email") or "").strip().lower() or None
    if email_n:
        other = (
            db.query(AdminUser)
            .filter(AdminUser.email == email_n, AdminUser.id != target.id)
            .first()
        )
        if other:
            request.session["flash_err"] = "Email already in use."
            return RedirectResponse("/users", status_code=303)
    target.email = email_n
    if target.id != user.id:
        target.is_platform_admin = form.get("is_platform_admin") == "on"
    new_pw = str(form.get("new_password") or "")
    if new_pw:
        if len(new_pw) < 8:
            request.session["flash_err"] = "Password min 8 characters."
            return RedirectResponse("/users", status_code=303)
        target.password_hash = hash_password(new_pw)
        target.must_change_password = form.get("must_change_password") == "on"
    elif form.get("must_change_password") == "on":
        target.must_change_password = True
    write_audit(
        db,
        actor=user,
        action="user.update",
        entity_type="user",
        entity_id=target.id,
        detail=f"email={email_n or '-'}",
    )
    db.commit()
    request.session["flash_ok"] = f"Updated {target.username}."
    return RedirectResponse("/users", status_code=303)


@router.post("/users/{user_id}/send-reset")
def users_send_reset(
    user_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from .accounts import create_reset_token
    from ..mailer import MailError, get_smtp, send_mail, smtp_ready

    target = db.get(AdminUser, user_id)
    if not target or not target.email:
        request.session["flash_err"] = "User needs an email address."
        return RedirectResponse("/users", status_code=303)
    cfg = get_smtp(db)
    if not smtp_ready(cfg):
        request.session["flash_err"] = "Configure SMTP first (/smtp)."
        return RedirectResponse("/users", status_code=303)
    try:
        raw = create_reset_token(db, target, by_admin=True)
        assert cfg is not None
        link = f"{cfg.public_base_url.rstrip('/')}/reset?token={raw}"
        send_mail(
            db,
            to_email=target.email,
            subject="Password reset — LLM Gateway",
            body_text=(
                f"Hi {target.username},\n\n"
                f"An admin requested a password reset. Link (1 hour):\n{link}\n"
            ),
        )
        write_audit(
            db,
            actor=user,
            action="user.send_reset",
            entity_type="user",
            entity_id=target.id,
        )
        db.commit()
        request.session["flash_ok"] = f"Reset mail sent to {target.email}."
    except MailError as exc:
        request.session["flash_err"] = str(exc)
    return RedirectResponse("/users", status_code=303)


# ---- Usage ----


@router.get("/usage", response_class=HTMLResponse)
def usage_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
    service: str | None = None,
    team_id: int | None = None,
    key_id: int | None = None,
    result: str | None = None,
):
    teams_on = _teams_on(db)
    q = db.query(UsageEvent).order_by(UsageEvent.created_at.desc())
    tids = user_team_ids(user)
    if not user.is_platform_admin:
        if teams_on:
            if not tids:
                q = q.filter(False)
            else:
                q = q.filter(UsageEvent.team_id.in_(tids))
                if team_id and team_id not in tids:
                    raise Forbidden()
        else:
            owned = [
                kid
                for (kid,) in db.query(ApiKey.id)
                .filter(ApiKey.owner_user_id == user.id)
                .all()
            ]
            q = q.filter(UsageEvent.api_key_id.in_(owned)) if owned else q.filter(False)
    if service:
        q = q.filter(UsageEvent.service == service)
    if team_id and teams_on:
        q = q.filter(UsageEvent.team_id == team_id)
    if key_id:
        q = q.filter(UsageEvent.api_key_id == key_id)
    if result:
        q = q.filter(UsageEvent.result == result)
    events = q.limit(200).all()
    teams = (
        (
            db.query(Team).order_by(Team.name).all()
            if user.is_platform_admin
            else user_teams(user)
        )
        if teams_on
        else []
    )
    keys_q = scope_keys_query(db.query(ApiKey), user, teams_enabled=teams_on)
    return templates.TemplateResponse(
        request,
        "usage.html",
        {
            "user": user,
            "events": events,
            "services": source_names(db),
            "teams": teams,
            "keys": keys_q.order_by(ApiKey.label).all(),
            "filters": {
                "service": service or "",
                "team_id": team_id or "",
                "key_id": key_id or "",
                "result": result or "",
            },
            "nav": "usage",
            "is_admin": user.is_platform_admin,
            "teams_enabled": teams_on,
        },
    )


@router.get("/usage/export.csv")
def usage_export(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
    service: str | None = None,
    team_id: int | None = None,
    key_id: int | None = None,
    result: str | None = None,
):
    teams_on = _teams_on(db)
    q = db.query(UsageEvent).order_by(UsageEvent.created_at.desc())
    if not user.is_platform_admin:
        if teams_on:
            tids = user_team_ids(user)
            q = q.filter(UsageEvent.team_id.in_(tids)) if tids else q.filter(False)
        else:
            owned = [
                kid
                for (kid,) in db.query(ApiKey.id)
                .filter(ApiKey.owner_user_id == user.id)
                .all()
            ]
            q = q.filter(UsageEvent.api_key_id.in_(owned)) if owned else q.filter(False)
    if service:
        q = q.filter(UsageEvent.service == service)
    if team_id and teams_on:
        q = q.filter(UsageEvent.team_id == team_id)
    if key_id:
        q = q.filter(UsageEvent.api_key_id == key_id)
    if result:
        q = q.filter(UsageEvent.result == result)
    events = q.limit(5000).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "created_at",
            "team",
            "key",
            "service",
            "method",
            "path",
            "host",
            "client_ip",
            "model",
            "status",
            "result",
            "duration_ms",
            "tokens_in",
            "tokens_out",
            "audio_seconds",
            "response_chars",
            "is_demo",
        ]
    )
    for e in events:
        writer.writerow(
            [
                e.created_at.isoformat() if e.created_at else "",
                e.team_name,
                e.key_label,
                e.service,
                e.method,
                e.path,
                e.host,
                e.client_ip,
                e.model or "",
                e.status,
                e.result,
                e.duration_ms or "",
                e.tokens_in or "",
                e.tokens_out or "",
                e.audio_seconds or "",
                e.response_chars or "",
                1 if e.is_demo else 0,
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=usage.csv"},
    )


# ---- Model catalog ----


@router.get("/models", response_class=HTMLResponse)
def models_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.catalog import (
        TAG_SUGGESTIONS,
        catalog_grouped_by_kind,
        format_bytes,
        format_param_count,
        list_catalog,
        suggest_docs_url,
    )

    flash_ok = request.session.pop("flash_ok", None)
    flash_err = request.session.pop("flash_err", None)
    rows = list_catalog(db)
    return templates.TemplateResponse(
        request,
        "models.html",
        {
            "user": user,
            "nav": "models",
            "rows": rows,
            "groups": catalog_grouped_by_kind(rows),
            "suggest_docs_url": suggest_docs_url,
            "format_param_count": format_param_count,
            "format_bytes": format_bytes,
            "tag_suggestions": TAG_SUGGESTIONS,
            "flash_ok": flash_ok,
            "flash_err": flash_err,
        },
    )


@router.post("/models/sync")
def models_sync(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.catalog import sync_catalog_from_sources

    stats = sync_catalog_from_sources(db)
    write_audit(
        db,
        actor=user,
        action="catalog.sync",
        entity_type="catalog_models",
        detail=str(stats),
    )
    db.commit()
    request.session["flash_ok"] = (
        f"Synced: {stats['seen']} models from {stats['sources']} sources "
        f"({stats['created']} new, {stats.get('tagged', 0)} auto-tagged, "
        f"{stats.get('meta', 0)} with meta)."
    )
    return RedirectResponse("/models", status_code=303)


@router.post("/models/save")
async def models_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.catalog import list_catalog, update_catalog_meta

    form = await request.form()
    enabled_ids = {int(x) for x in form.getlist("enabled") if str(x).isdigit()}
    changed = 0
    for row in list_catalog(db):
        want = row.id in enabled_ids
        if row.enabled != want:
            row.enabled = want
            changed += 1
        update_catalog_meta(
            db,
            row.id,
            tags=str(form.get(f"tags_{row.id}") or ""),
            short_note=str(form.get(f"note_{row.id}") or ""),
            docs_url=str(form.get(f"docs_{row.id}") or ""),
        )
    write_audit(
        db,
        actor=user,
        action="catalog.update",
        entity_type="catalog_models",
        detail=f"toggled={changed}",
    )
    db.commit()
    request.session["flash_ok"] = f"Saved ({changed} enable toggles; metadata updated)."
    return RedirectResponse("/models", status_code=303)


# ---- Services / Settings ----


@router.get("/services", response_class=HTMLResponse)
def services_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    from ..data.backends import catalog_route_models, source_rows
    from ..data.probe import probe_all

    settings = _settings(request)
    rows = source_rows(db, settings)
    statuses = probe_all(db)
    catalog_by_source = {src.name: catalog_route_models(db, src.name) for src, _ in rows}
    flash_ok = request.session.pop("flash_ok", None)
    flash_err = request.session.pop("flash_err", None)
    return templates.TemplateResponse(
        request,
        "services.html",
        {
            "user": user,
            "rows": rows,
            "statuses": statuses,
            "catalog_by_source": catalog_by_source,
            "kinds": KINDS,
            "api_styles": dialect_choices(),
            "dialect_blurbs": {k: dialect_blurb_for_kind(k) for k in KINDS},
            "domain": settings.domain,
            "nav": "services",
            "flash_ok": flash_ok,
            "flash_err": flash_err,
            "can_edit": user.is_platform_admin,
        },
    )


@router.get("/services/status")
def services_status_json(
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    from ..data.probe import probe_all

    return {"services": [s.to_dict() for s in probe_all(db)]}


@router.post("/services")
def services_create(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    name: str = Form(""),
    kind: str = Form("chat"),
    address: str = Form(""),
    is_default: str = Form(""),
    isolated: str = Form(""),
    api_style: str = Form("auto"),
):
    from ..data.backends import (
        upsert_source,
        validate_backend,
        validate_kind,
        validate_source_name,
    )

    err = validate_source_name(name) or validate_kind(kind) or validate_backend(address)
    if err:
        request.session["flash_err"] = err
        return RedirectResponse("/services", status_code=303)
    if get_source_by_name(db, name.strip().lower()):
        request.session["flash_err"] = f"Source '{name}' already exists — edit address below"
        return RedirectResponse("/services", status_code=303)

    src = upsert_source(
        db,
        name=name,
        kind=kind,
        address=address,
        is_default=bool(is_default),
        route_models="",
        isolated=bool(isolated),
        api_style=api_style,
    )
    write_audit(
        db,
        actor=user,
        action="source.create",
        entity_type="backend_source",
        entity_id=src.id,
        detail=f"{src.name} kind={src.kind} default={src.is_default} isolated={src.isolated} style={src.api_style}",
    )
    db.commit()
    request.session["flash_ok"] = f"Source '{src.name}' added."
    return RedirectResponse("/services", status_code=303)


@router.post("/services/{source_id}/save")
def services_update(
    source_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    address: str = Form(""),
    is_default: str = Form(""),
    isolated: str = Form(""),
    api_style: str = Form("auto"),
):
    from ..data.backends import (
        clear_default_for_kind,
        normalize_backend,
        validate_backend,
    )
    from ..data.models import BackendSource

    src = db.get(BackendSource, source_id)
    if src is None:
        request.session["flash_err"] = "Source not found"
        return RedirectResponse("/services", status_code=303)
    err = validate_backend(address)
    if err:
        request.session["flash_err"] = f"{src.name}: {err}"
        return RedirectResponse("/services", status_code=303)

    style = (api_style or "auto").strip().lower()
    if style not in API_STYLES:
        style = "auto"
    src.address = normalize_backend(address)
    src.route_models = ""  # merge targets come from catalog sync only
    src.isolated = bool(isolated)
    src.api_style = style
    make_default = bool(is_default)
    if make_default:
        clear_default_for_kind(db, src.kind, except_id=src.id)
        src.is_default = True
    elif src.is_default and not make_default:
        pass
    write_audit(
        db,
        actor=user,
        action="source.update",
        entity_type="backend_source",
        entity_id=src.id,
        detail=(
            f"{src.name} addr={'set' if src.address else 'empty'} "
            f"default={src.is_default} isolated={src.isolated} style={src.api_style}"
        ),
    )
    db.commit()
    request.session["flash_ok"] = f"Source '{src.name}' saved."
    return RedirectResponse("/services", status_code=303)


@router.post("/services/{source_id}/delete")
def services_delete(
    source_id: int,
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
):
    from ..data.backends import delete_source
    from ..data.models import BackendSource

    src = db.get(BackendSource, source_id)
    if src is None:
        request.session["flash_err"] = "Source not found"
        return RedirectResponse("/services", status_code=303)
    name = src.name
    delete_source(db, src)
    write_audit(
        db,
        actor=user,
        action="source.delete",
        entity_type="backend_source",
        entity_id=source_id,
        detail=name,
    )
    db.commit()
    request.session["flash_ok"] = f"Source '{name}' deleted."
    return RedirectResponse("/services", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_user)],
):
    import os

    from ..data.backends import source_rows
    from .accounts import get_auth_settings

    settings = _settings(request)
    public_host = request.headers.get("x-forwarded-host") or request.headers.get("host") or "127.0.0.1"
    host_only = public_host.split(":")[0]
    admin_url = str(request.base_url).rstrip("/")
    gw_port = os.getenv("GATEWAY_PORT", "9081")
    gateway_url = f"http://{host_only}:{gw_port}"

    backend_rows_data = source_rows(db, settings)
    auth = get_auth_settings(db) if user.is_platform_admin else None
    teams = (
        db.query(Team).order_by(Team.name).all() if user.is_platform_admin else []
    )
    flash_ok = request.session.pop("flash_ok", None)
    flash_err = request.session.pop("flash_err", None)
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "user": user,
            "settings": settings,
            "nav": "settings",
            "session_max_age": settings.session_max_age,
            "temp_max_c": settings.temp_max_c,
            "temp_guard_disabled": settings.temp_guard_disabled,
            "admin_url": admin_url,
            "gateway_url": gateway_url,
            "backend_rows": backend_rows_data,
            "auth": auth,
            "teams": teams,
            "flash_ok": flash_ok,
            "flash_err": flash_err,
        },
    )


@router.post("/settings/auth")
def settings_auth_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[AdminUser, Depends(require_platform_admin)],
    allow_self_registration: str = Form(""),
    require_email: str = Form(""),
    teams_enabled: str = Form(""),
    default_team_id: str = Form(""),
    anonymize_client_ip: str = Form(""),
    retention_days: str = Form("30"),
    auto_vl_routing: str = Form(""),
):
    from .accounts import get_auth_settings
    from ..privacy import purge_old_usage

    auth = get_auth_settings(db)
    auth.allow_self_registration = allow_self_registration == "on"
    auth.require_email = require_email == "on"
    auth.teams_enabled = teams_enabled == "on"
    auth.anonymize_client_ip = anonymize_client_ip == "on"
    auth.auto_vl_routing = auto_vl_routing == "on"
    try:
        auth.retention_days = max(0, min(3650, int(retention_days or "30")))
    except ValueError:
        auth.retention_days = 30
    tid = default_team_id.strip()
    if auth.teams_enabled and tid:
        team = db.get(Team, int(tid))
        auth.default_team_id = team.id if team else None
    else:
        auth.default_team_id = None
    purged = purge_old_usage(db, auth.retention_days)
    write_audit(
        db,
        actor=user,
        action="settings.auth",
        entity_type="auth_settings",
        entity_id=auth.id,
        detail=(
            f"register={auth.allow_self_registration} "
            f"teams={auth.teams_enabled} "
            f"anon_ip={auth.anonymize_client_ip} "
            f"auto_vl={auth.auto_vl_routing} "
            f"retention={auth.retention_days} purged={purged}"
        ),
    )
    db.commit()
    request.session["flash_ok"] = (
        f"Settings saved. Purged {purged} old usage events."
        if purged
        else "Auth / privacy settings saved."
    )
    return RedirectResponse("/settings", status_code=303)
