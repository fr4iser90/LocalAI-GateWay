from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from ...data.db import get_db, verify_password
from ..session import current_user
from ..shared import templates

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Annotated[Session, Depends(get_db)]):
    if current_user(request, db):
        return RedirectResponse("/", status_code=303)
    from ..accounts import get_auth_settings

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
    from ..accounts import find_user_by_login, get_auth_settings

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
    from ..accounts import user_needs_onboarding

    request.session["user_id"] = user.id
    if user_needs_onboarding(user):
        return RedirectResponse("/account", status_code=303)
    if user.is_platform_admin:
        from ..setup import needs_setup_wizard, wizard_progress

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
