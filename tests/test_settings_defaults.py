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
    assert (auth.operator_email or "") in {"", "support@fr4iser.com"}


def test_operator_env_maps_on_settings(monkeypatch):
    from app.config import Settings, get_settings

    monkeypatch.setenv("OPERATOR_NAME", "Patrick Böhme")
    monkeypatch.setenv("OPERATOR_ADDRESS", "Georg-Maurer-Straße 17, 04279 Leipzig")
    monkeypatch.setenv("OPERATOR_EMAIL", "support@fr4iser.com")
    get_settings.cache_clear()
    try:
        s = Settings()
        assert s.operator_name == "Patrick Böhme"
        assert "04279 Leipzig" in s.operator_address
        assert s.operator_email == "support@fr4iser.com"
    finally:
        get_settings.cache_clear()
