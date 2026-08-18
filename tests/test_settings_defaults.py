from __future__ import annotations

from pathlib import Path

from app.data.models import AuthSettings, Base, make_engine, make_session_factory


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "settings.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_auth_settings_defaults_global_stats_off(tmp_path: Path):
    db = _session(tmp_path)
    auth = AuthSettings()
    db.add(auth)
    db.flush()
    assert auth.show_global_stats is False
