"""Shared password rules for UI + server (bcrypt max useful length is 72 bytes)."""

from __future__ import annotations

import re

PASSWORD_MIN_LEN = 8
PASSWORD_MAX_LEN = 72  # bcrypt silently truncates beyond 72 bytes

_RE_LOWER = re.compile(r"[a-z]")
_RE_UPPER = re.compile(r"[A-Z]")
_RE_DIGIT = re.compile(r"\d")
_RE_SYMBOL = re.compile(r"[^A-Za-z0-9]")


def password_checks(password: str) -> dict[str, bool]:
    pw = password or ""
    return {
        "length": PASSWORD_MIN_LEN <= len(pw) <= PASSWORD_MAX_LEN,
        "lower": bool(_RE_LOWER.search(pw)),
        "upper": bool(_RE_UPPER.search(pw)),
        "digit": bool(_RE_DIGIT.search(pw)),
        "symbol": bool(_RE_SYMBOL.search(pw)),
    }


def password_strength_score(password: str) -> int:
    """0–4 from character-class coverage (length counted separately)."""
    c = password_checks(password)
    return sum(1 for k in ("lower", "upper", "digit", "symbol") if c[k])


def validate_new_password(password: str, password2: str) -> str | None:
    """Return error message or None if OK."""
    if password != password2:
        return "New passwords must match."
    if len(password) < PASSWORD_MIN_LEN:
        return f"Password must be at least {PASSWORD_MIN_LEN} characters."
    if len(password) > PASSWORD_MAX_LEN:
        return f"Password must be at most {PASSWORD_MAX_LEN} characters."
    c = password_checks(password)
    if not c["lower"] and not c["upper"]:
        return "Password must include at least one letter."
    if not c["digit"]:
        return "Password must include at least one digit."
    return None


def policy_for_template() -> dict:
    return {
        "min_len": PASSWORD_MIN_LEN,
        "max_len": PASSWORD_MAX_LEN,
    }
