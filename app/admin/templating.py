"""Jinja helpers — inject teams_enabled into every logged-in page."""

from __future__ import annotations

from fastapi.templating import Jinja2Templates
from pathlib import Path


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    original = templates.TemplateResponse

    def TemplateResponse(request, name, context=None, status_code=200, **kwargs):
        context = dict(context or {})
        if "teams_enabled" not in context:
            try:
                from ..data.db import SessionLocal
                from .accounts import get_auth_settings, operator_public

                if SessionLocal is not None:
                    db = SessionLocal()
                    try:
                        auth = get_auth_settings(db)
                        context["teams_enabled"] = bool(auth.teams_enabled)
                        context.setdefault("operator", operator_public(auth))
                        context.setdefault("auth", auth)
                    finally:
                        db.close()
                else:
                    context["teams_enabled"] = False
            except Exception:
                context["teams_enabled"] = False
        if "operator" not in context:
            context["operator"] = {
                "name": "",
                "address": "",
                "email": "support@fr4iser.com",
                "phone": "",
                "complete": False,
                "from_env": False,
            }
        if "display_tz" not in context and context.get("user") is not None:
            try:
                from ..stats import zone_from_request

                context["display_tz"] = str(
                    zone_from_request(request, context.get("user"))
                )
            except Exception:
                context["display_tz"] = "UTC"
        if "setup_incomplete" not in context:
            context["setup_incomplete"] = bool(
                getattr(request.state, "setup_incomplete", False)
            )
        if "pw_policy" not in context:
            try:
                from ..password_policy import policy_for_template

                context["pw_policy"] = policy_for_template()
            except Exception:
                context["pw_policy"] = {"min_len": 8, "max_len": 72}
        return original(
            request, name, context, status_code=status_code, **kwargs
        )

    templates.TemplateResponse = TemplateResponse  # type: ignore[method-assign]
    return templates
