"""Budget weight suggestions from usage (optional, never auto-applied)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.admin.user_limits import user_limits_summary
from app.data.models import AdminUser, Base, CatalogModel, UsageEvent, make_engine, make_session_factory, utcnow
from app.data.usage_weights import catalog_weight_suggestions


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "w.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _add_tg_events(db, service: str, model: str, tg: float, n: int = 4) -> None:
    now = utcnow()
    for i in range(n):
        db.add(
            UsageEvent(
                service=service,
                model=model,
                result="ok",
                tg_tok_s=tg,
                created_at=now - timedelta(hours=i),
            )
        )


def test_suggestions_not_ready_without_data(tmp_path: Path):
    db = _session(tmp_path)
    status = catalog_weight_suggestions(db)
    assert status.ready is False
    assert "Need TG tok/s" in status.message


def test_suggestions_ready_and_weights(tmp_path: Path):
    db = _session(tmp_path)
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="fast", enabled=True))
    db.add(CatalogModel(source_name="chat", kind="chat", model_id="slow", enabled=True))
    _add_tg_events(db, "chat", "fast", 100.0)
    _add_tg_events(db, "chat", "slow", 50.0)
    db.commit()
    status = catalog_weight_suggestions(db)
    assert status.ready is True
    by_model = {s.model_id: s for s in status.suggestions}
    assert by_model["fast"].suggested_weight == 0.8
    assert by_model["slow"].suggested_weight == 1.5


def test_user_limits_summary_empty_means_unlimited(tmp_path: Path):
    db = _session(tmp_path)
    u = AdminUser(username="bob", password_hash="x", is_platform_admin=False)
    db.add(u)
    db.commit()
    assert user_limits_summary(u) == "No limits (∞)"
