"""Dashboard attention helpers."""

from __future__ import annotations

from app.admin.dashboard_ops import attention_items
from app.data.models import AdminUser, Base, make_engine, make_session_factory


def _db(tmp_path):
    eng = make_engine(str(tmp_path / "dash.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_attention_flags_smtp_and_no_sources(tmp_path):
    db = _db(tmp_path)
    items = attention_items(db, fleet=[], denies_24h=0, rate_limits_24h=0)
    titles = {i.title for i in items}
    assert "No backends configured" in titles
    assert "SMTP not configured" in titles


def test_attention_source_down(tmp_path):
    db = _db(tmp_path)
    from app.data.probe import ServiceStatus

    fleet = [
        ServiceStatus(service="chat", backend="127.0.0.1:1", state="down", detail="timeout")
    ]
    items = attention_items(db, fleet=fleet, denies_24h=0, rate_limits_24h=0)
    assert any("chat" in i.title for i in items)


def test_attention_pool_near_limit(tmp_path):
    from app.data.db import hash_password

    db = _db(tmp_path)
    from app.admin.accounts import get_auth_settings

    auth = get_auth_settings(db)
    auth.pool_window_hours = 5
    u = AdminUser(
        username="heavy",
        password_hash=hash_password("x"),
        is_active=True,
        pool_limit=100,
        pool_used=92.0,
    )
    db.add(u)
    db.commit()
    items = attention_items(db, fleet=[], denies_24h=0, rate_limits_24h=0)
    assert any("heavy" in i.title for i in items)
