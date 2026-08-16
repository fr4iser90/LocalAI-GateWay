"""Pytest fixtures. Unit tests never require LAN backends."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture()
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SESSION_SECRET", "test-session-secret-not-for-prod")
    monkeypatch.setenv("ADMIN_BOOTSTRAP_PASSWORD", "test-admin-pass")
    monkeypatch.setenv("DOMAIN", "example.test")
    monkeypatch.setenv("TEMP_GUARD_DISABLED", "true")
    # Clear cached settings between tests
    from app.config import get_settings

    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: hits real sources from env (CHAT_SOURCE etc.); skipped in CI",
    )
