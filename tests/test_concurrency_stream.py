"""Concurrency held until explicit release (stream lifetime)."""

from __future__ import annotations

from unittest.mock import MagicMock

from app.auth import concurrency as conc_mod
from app.auth.concurrency import ConcurrencyLease, release_concurrency_lease
from app.auth.rate_limit import RateLimiter


def test_lease_blocks_until_released(monkeypatch):
    lim = RateLimiter()
    monkeypatch.setattr(conc_mod, "rate_limiter", lim)
    monkeypatch.setattr(conc_mod, "priority_gate", MagicMock())

    d1 = lim.check_and_acquire(
        key_id=1,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_concurrency=1,
    )
    assert d1.allowed

    d2 = lim.check_and_acquire(
        key_id=2,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_concurrency=1,
    )
    assert not d2.allowed
    assert d2.reason == "user_concurrency_exceeded"

    release_concurrency_lease(ConcurrencyLease(key_id=1, user_id=9))

    d3 = lim.check_and_acquire(
        key_id=2,
        team_id=None,
        rpm=None,
        concurrency=None,
        user_id=9,
        user_concurrency=1,
    )
    assert d3.allowed
    release_concurrency_lease(ConcurrencyLease(key_id=2, user_id=9))
