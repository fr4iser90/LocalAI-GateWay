"""Password policy validation."""

from app.password_policy import (
    PASSWORD_MAX_LEN,
    PASSWORD_MIN_LEN,
    password_checks,
    password_strength_score,
    validate_new_password,
)


def test_limits():
    assert PASSWORD_MIN_LEN == 8
    assert PASSWORD_MAX_LEN == 72


def test_validate_ok():
    assert validate_new_password("Abcdefg1", "Abcdefg1") is None
    assert validate_new_password("abc12345!", "abc12345!") is None


def test_validate_errors():
    assert "match" in (validate_new_password("Abcdefg1", "Abcdefg2") or "").lower()
    assert "at least" in (validate_new_password("Ab1", "Ab1") or "").lower()
    assert "digit" in (validate_new_password("Abcdefgh", "Abcdefgh") or "").lower()
    assert "letter" in (validate_new_password("12345678", "12345678") or "").lower()
    long = "a1" + ("x" * 71)
    assert "at most" in (validate_new_password(long, long) or "").lower()


def test_checks_and_score():
    c = password_checks("Aa1!")
    assert c["lower"] and c["upper"] and c["digit"] and c["symbol"]
    assert password_strength_score("Aa1!") == 4
    assert password_strength_score("aaaaaaa1") == 2
