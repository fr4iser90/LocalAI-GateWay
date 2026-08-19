"""Audit hash chain — stable timestamps + verify."""

from datetime import datetime, timezone

from app.audit import (
    GENESIS,
    _audit_payload,
    _audit_ts,
    verify_audit_chain,
    write_audit,
)
from app.crypto_util import hash_audit_chain
from app.data.models import WebUser, AuditLog, Base, make_engine, make_session_factory


def _db(tmp_path):
    eng = make_engine(str(tmp_path / "audit.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_audit_ts_canonical():
    dt = datetime(2026, 8, 16, 12, 40, 40, 387205, tzinfo=timezone.utc)
    assert _audit_ts(dt) == "2026-08-16T12:40:40.387205Z"
    naive = datetime(2026, 8, 16, 12, 40, 40, 387205)
    assert _audit_ts(naive).endswith("Z")


def test_write_and_verify_chain(tmp_path):
    db = _db(tmp_path)
    admin = WebUser(
        username="admin",
        password_hash="x",
        is_platform_admin=True,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    write_audit(db, actor=admin, action="setup.key", entity_type="api_key", entity_id=1, detail="a")
    write_audit(db, actor=admin, action="setup.catalog.sync", entity_type="catalog_models", detail="b")
    db.commit()
    ok, msg = verify_audit_chain(db)
    assert ok, msg
    assert "2 rows" in msg


def test_legacy_isoformat_still_verifies(tmp_path):
    """Rows hashed with datetime.isoformat() before the canonical fix."""
    db = _db(tmp_path)
    created = datetime(2026, 8, 16, 12, 40, 40, 387205, tzinfo=timezone.utc)
    payload = _audit_payload(
        created_ts=created.isoformat(),
        actor_user_id=1,
        actor_username="admin",
        action="test",
        entity_type="x",
        entity_id="1",
        detail="d",
    )
    entry = hash_audit_chain(GENESIS, payload)
    db.add(
        AuditLog(
            actor_user_id=1,
            actor_username="admin",
            action="test",
            entity_type="x",
            entity_id="1",
            detail="d",
            created_at=created,
            prev_hash=GENESIS,
            entry_hash=entry,
        )
    )
    db.commit()
    ok, msg = verify_audit_chain(db)
    assert ok, msg
