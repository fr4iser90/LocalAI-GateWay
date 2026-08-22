"""Concurrency lease releases source admission."""

from __future__ import annotations

from app.auth.concurrency import ConcurrencyLease, release_concurrency_lease
from app.auth.source_admission import SourceAdmissionGate
import app.auth.source_admission as admission_mod


def test_release_frees_source_slot():
    gate = SourceAdmissionGate()
    orig = admission_mod.source_admission_gate
    admission_mod.source_admission_gate = gate
    try:
        assert gate.acquire("h:9", limit=1, key_id=1, priority=0, timeout=1.0)
        lease = ConcurrencyLease(key_id=1, source_key="h:9")
        release_concurrency_lease(lease)
        assert gate.acquire("h:9", limit=1, key_id=2, priority=0, timeout=0.1)
        gate.release("h:9")
    finally:
        admission_mod.source_admission_gate = orig
