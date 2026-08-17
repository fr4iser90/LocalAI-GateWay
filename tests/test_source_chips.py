"""Source chip helpers + default grant (non-isolated)."""

from __future__ import annotations

from pathlib import Path

from app.data.backends import (
    default_grant_source_names,
    source_chip_rows,
    upsert_source,
)
from app.data.models import Base, make_engine, make_session_factory


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "chips.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_default_grant_skips_isolated(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="10.0.0.1:1", is_default=True)
    upsert_source(
        db, name="chat2", kind="chat", address="10.0.0.1:2", is_default=False, isolated=True
    )
    upsert_source(db, name="embed", kind="embed", address="10.0.0.1:3", is_default=True)
    db.commit()
    names = default_grant_source_names(db)
    assert "chat" in names
    assert "embed" in names
    assert "chat2" not in names


def test_source_chip_rows_hints(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="h:1", is_default=True)
    upsert_source(
        db, name="lab", kind="chat", address="h:2", is_default=False, isolated=True
    )
    db.commit()
    rows = source_chip_rows(db)
    by = {r["name"]: r for r in rows}
    assert by["chat"]["is_default"]
    assert "Daily" in by["chat"]["hint"]
    assert by["lab"]["isolated"]
    assert "Lab" in by["lab"]["hint"]
    filtered = source_chip_rows(db, ["chat"])
    assert [r["name"] for r in filtered] == ["chat"]
