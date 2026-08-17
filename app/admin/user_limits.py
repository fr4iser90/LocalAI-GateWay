"""User-level rate limits + token budget (display helpers)."""

from __future__ import annotations

from ..data.models import AdminUser


def opt_int_limit(raw: str) -> int | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        n = int(s)
    except ValueError:
        return None
    return n if n > 0 else None


def apply_user_limits_from_form(target: AdminUser, form) -> None:
    target.rpm_limit = opt_int_limit(str(form.get("rpm_limit") or ""))
    target.concurrency_limit = opt_int_limit(str(form.get("concurrency_limit") or ""))
    target.daily_quota = opt_int_limit(str(form.get("daily_quota") or ""))
    target.pool_limit = opt_int_limit(str(form.get("pool_limit") or ""))
    if str(form.get("pool_reset_now") or "") == "on":
        target.pool_used = 0.0
        from ..data.models import utcnow

        target.pool_window_start = utcnow()


def user_limits_summary(user: AdminUser, *, pool_window_hours: int = 0) -> str:
    """One-line summary for the users table."""
    if user.is_platform_admin:
        return "full access"
    parts: list[str] = []
    if user.rpm_limit:
        parts.append(f"{user.rpm_limit} RPM")
    if user.concurrency_limit:
        parts.append(f"conc {user.concurrency_limit}")
    if user.daily_quota:
        parts.append(f"{user.daily_quota}/day")
    if user.pool_limit:
        win = f"/{pool_window_hours}h" if pool_window_hours else ""
        used = int(user.pool_used or 0)
        parts.append(f"{used}/{user.pool_limit} tok{win}")
    return " · ".join(parts) if parts else "No limits (∞)"
