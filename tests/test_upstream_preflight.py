"""Upstream preflight before proxy."""

from __future__ import annotations

from unittest.mock import patch

from app.data.source_load import SourceLoadSnapshot
from app.upstream_preflight import preflight_upstream


def test_preflight_loading_from_snapshot():
    snap = SourceLoadSnapshot(state="loading", probed_at=1.0)
    with patch("app.upstream_preflight.probe_source_load", return_value=snap):
        pf = preflight_upstream(backend="127.0.0.1:8080", kind="chat", model="jarvis")
    assert not pf.ok
    assert pf.reason == "model_initializing"


def test_preflight_busy():
    snap = SourceLoadSnapshot(state="busy", probed_at=1.0)
    with patch("app.upstream_preflight.probe_source_load", return_value=snap):
        pf = preflight_upstream(backend="127.0.0.1:8080", kind="chat", model="jarvis")
    assert not pf.ok
    assert pf.reason == "backend_busy"
    assert pf.retry_after == 5


def test_preflight_model_not_loaded():
    snap = SourceLoadSnapshot(state="ok", model_loaded=False, probed_at=1.0)
    with patch("app.upstream_preflight.probe_source_load", return_value=snap):
        pf = preflight_upstream(backend="127.0.0.1:11434", kind="chat", model="jarvis")
    assert not pf.ok
    assert pf.reason == "model_initializing"


def test_preflight_fail_open_on_down():
    snap = SourceLoadSnapshot(state="down", probed_at=1.0)
    with patch("app.upstream_preflight.probe_source_load", return_value=snap):
        pf = preflight_upstream(backend="127.0.0.1:8080", kind="chat", model="jarvis")
    assert pf.ok
