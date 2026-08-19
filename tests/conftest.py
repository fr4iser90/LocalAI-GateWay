"""Pytest fixtures. Unit tests never require LAN backends or the developer .env."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.constants import (
    BOOTSTRAP_PASSWORD,
    BOOTSTRAP_USER,
    TEST_DOMAIN,
    TEST_SESSION_SECRET,
)


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated DATA_DIR + fixed bootstrap env (overrides shell / --env-file)."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", TEST_SESSION_SECRET)
    monkeypatch.setenv("ADMIN_BOOTSTRAP_USER", BOOTSTRAP_USER)
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", BOOTSTRAP_PASSWORD)
    monkeypatch.setenv("DOMAIN", TEST_DOMAIN)
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "true")
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: hits real sources from env (CHAT_SOURCE etc.); skipped in CI",
    )
    config.addinivalue_line(
        "markers",
        "security: HTTP authz / escalation checks against the full app",
    )


@pytest.fixture()
def gateway_client(data_dir: Path):
    """FastAPI app + TestClient on an isolated DATA_DIR (no LAN backends)."""
    from starlette.testclient import TestClient

    from app.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as client:
        yield client
    get_settings.cache_clear()
