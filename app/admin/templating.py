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
                from .accounts import teams_feature_enabled

                if SessionLocal is not None:
                    db = SessionLocal()
                    try:
                        context["teams_enabled"] = teams_feature_enabled(db)
                    finally:
                        db.close()
                else:
                    context["teams_enabled"] = False
            except Exception:
                context["teams_enabled"] = False
        if "display_tz" not in context and context.get("user") is not None:
            try:
                from ..stats import zone_from_request

                context["display_tz"] = str(
                    zone_from_request(request, context.get("user"))
                )
            except Exception:
                context["display_tz"] = "UTC"
        return original(
            request, name, context, status_code=status_code, **kwargs
        )

    templates.TemplateResponse = TemplateResponse  # type: ignore[method-assign]
    return templates
