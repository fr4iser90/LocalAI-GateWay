"""User ceilings + max keys per user."""

from __future__ import annotations

from pathlib import Path

from app.web.accounts import assert_can_create_key, get_auth_settings, max_keys_allowed
from app.auth.rate_limit import RateLimiter
from app.data.db import hash_api_key
from app.data.models import (
    WebUser,
    ApiKey,
    AuthSettings,
    Base,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "lim.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _user(db, name: str, *, admin: bool = False, **lim):
    u = WebUser(
        username=name,
        password_hash="x",
        is_platform_admin=admin,
        is_active=True,
        **lim,
    )
    db.add(u)
    db.flush()
    return u


def _key(db, owner: WebUser, label: str = "k"):
    raw = f"gw_test_{label}"
    k = ApiKey(
        label=label,
        key_hash=hash_api_key(raw),
        key_prefix="gw_test",
        owner_user_id=owner.id,
        is_active=True,
    )
    db.add(k)
    db.flush()
    return k


def test_max_keys_global_default(tmp_path: Path):
    db = _session(tmp_path)
    db.add(AuthSettings(max_keys_per_user=3))
    u = _user(db, "friend")
    admin = _user(db, "admin", admin=True)
    db.commit()

    assert max_keys_allowed(db, u) == 3
    assert max_keys_allowed(db, admin) is None

    _key(db, u, "a")
    _key(db, u, "b")
    _key(db, u, "c")
    db.commit()
    err = assert_can_create_key(db, u)
    assert err is not None
    assert "limit 3" in err
    assert assert_can_create_key(db, admin) is None


def test_max_keys_zero_means_unlimited(tmp_path: Path):
    db = _session(tmp_path)
    auth = AuthSettings(max_keys_per_user=0)
    db.add(auth)
    u = _user(db, "friend")
    db.commit()
    assert max_keys_allowed(db, u) is None
    assert get_auth_settings(db).max_keys_per_user == 0


def test_user_rpm_across_keys():
    lim = RateLimiter()
    # user rpm=2 across keys
    d1 = lim.check_and_acquire(
        key_id=1,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_rpm=2,
    )
    assert d1.allowed
    lim.release(1, user_id=9)
    d2 = lim.check_and_acquire(
        key_id=2,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_rpm=2,
    )
    assert d2.allowed
    lim.release(2, user_id=9)
    d3 = lim.check_and_acquire(
        key_id=1,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_rpm=2,
    )
    assert not d3.allowed
    assert d3.reason == "user_rpm_exceeded"
