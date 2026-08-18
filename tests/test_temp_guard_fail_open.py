from __future__ import annotations

from types import SimpleNamespace
from pathlib import Path

import pytest

from app.data.models import BackendSource, Base, make_engine, make_session_factory


def _clear_settings_cache() -> None:
    from app.config import get_settings

    get_settings.cache_clear()


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "temp-guard.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _call_check_temperature(db, service: str = "chat"):
    _clear_settings_cache()
    from app.auth.check import check_temperature

    return check_temperature(db, service)


def _add_source(db, *, enabled: bool = True, address: str = "10.0.0.5:8080") -> None:
    db.add(
        BackendSource(
            name="chat",
            kind="chat",
            address=address,
            temp_guard_enabled=enabled,
        )
    )
    db.commit()


class _DummyClient:
    def __init__(self, exc: Exception | None = None, status_code: int = 204):
        self._exc = exc
        self._status_code = status_code

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def get(self, _url: str):
        if self._exc is not None:
            raise self._exc
        return SimpleNamespace(status_code=self._status_code)


def test_fail_open_unreachable_allows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "true")
    _clear_settings_cache()

    import app.auth.check as check_mod

    def client_factory(*_args, **_kwargs):
        return _DummyClient(exc=RuntimeError("boom"))

    monkeypatch.setattr(check_mod.httpx, "Client", client_factory)

    res = _call_check_temperature(db)
    assert res is None


def test_fail_closed_unreachable_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "false")
    _clear_settings_cache()

    import app.auth.check as check_mod

    def client_factory(*_args, **_kwargs):
        return _DummyClient(exc=RuntimeError("boom"))

    monkeypatch.setattr(check_mod.httpx, "Client", client_factory)

    res = _call_check_temperature(db)
    assert res is not None
    assert res.status == 503
    assert "temp_guard_unreachable" in res.reason


def test_fail_open_unexpected_status_allows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "true")
    _clear_settings_cache()

    import app.auth.check as check_mod

    def client_factory(*_args, **_kwargs):
        return _DummyClient(status_code=500)

    monkeypatch.setattr(check_mod.httpx, "Client", client_factory)

    res = _call_check_temperature(db)
    assert res is None


def test_fail_closed_unexpected_status_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "false")
    _clear_settings_cache()

    import app.auth.check as check_mod

    def client_factory(*_args, **_kwargs):
        return _DummyClient(status_code=500)

    monkeypatch.setattr(check_mod.httpx, "Client", client_factory)

    res = _call_check_temperature(db)
    assert res is not None
    assert res.status == 503
    assert "temp_guard_status_500" in res.reason


def test_403_blocks_even_when_fail_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "true")
    _clear_settings_cache()

    import app.auth.check as check_mod

    def client_factory(*_args, **_kwargs):
        return _DummyClient(status_code=403)

    monkeypatch.setattr(check_mod.httpx, "Client", client_factory)

    res = _call_check_temperature(db)
    assert res is not None
    assert res.status == 503
    assert res.reason == "local_temperature_above_limit"


def test_disabled_per_source_skips_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _session(tmp_path)
    _add_source(db, enabled=False)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "false")
    monkeypatch.setenv("TEMP_GUARD_FAIL_OPEN", "false")

    res = _call_check_temperature(db)
    assert res is None

