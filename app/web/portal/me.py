from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session, joinedload

from ...config import public_api_base
from ...data.db import get_db
from ...data.models import WebUser, ApiKey
from ..session import require_user, scope_keys_query
from ..shared import templates, _teams_on

router = APIRouter()


@router.get("/me", response_class=HTMLResponse)
def me_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[WebUser, Depends(require_user)],
):
    from ...stats import area_chart_svg, pulse_stats

    teams_on = _teams_on(db)
    keys_q = scope_keys_query(
        db.query(ApiKey).options(
            joinedload(ApiKey.team),
        ),
        user,
        teams_enabled=teams_on,
    )
    keys = keys_q.order_by(ApiKey.label).all()
    key_ids = [k.id for k in keys]
    from ...config import onprem_api_port, public_api_base
    import os

    api_base = public_api_base()
    flash_ok = request.session.pop("flash_ok", None)
    pulse = pulse_stats(db, key_ids=key_ids)
    return templates.TemplateResponse(
        request,
        "me.html",
        {
            "user": user,
            "api_base": api_base,
            "keys": keys,
            "flash_ok": flash_ok,
            "pulse": pulse,
            "pulse_status": "Healthy" if pulse["count"] else "Idle",
            "pulse_cta": {"href": "/keys/new", "label": "New key"},
            "chart_pulse": area_chart_svg(
                pulse["series"], fill_id="gw-pulse", aria="Throughput last 60 minutes"
            ),
            "nav": "me",
            "is_admin": user.is_platform_admin,
            "teams_enabled": teams_on,
        },
    )
