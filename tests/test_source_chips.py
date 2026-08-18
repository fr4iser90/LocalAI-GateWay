"""Source chip helpers + default grant (kind-default sources)."""

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


def test_all_addressed_sources_are_equal(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="10.0.0.1:1")
    upsert_source(db, name="chat2", kind="chat", address="10.0.0.1:2")
    upsert_source(db, name="embed", kind="embed", address="10.0.0.1:3")
    db.commit()
    names = default_grant_source_names(db)
    assert names == ["chat", "chat2", "embed"]


def test_source_chip_rows_are_equal_sources(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="h:1", is_default=True)
    upsert_source(db, name="lab", kind="chat", address="h:2", is_default=False)
    db.commit()
    rows = source_chip_rows(db)
    by = {r["name"]: r for r in rows}
    assert by["chat"]["kind"] == "chat"
    assert by["lab"]["kind"] == "chat"
    assert "hardware" in by["chat"]
    assert "tooltip" in by["chat"]
    assert "extra" not in (by["lab"]["hint"] or "").lower()
    assert "fallback" not in (by["chat"]["hint"] or "").lower()
    filtered = source_chip_rows(db, ["chat"])
    assert [r["name"] for r in filtered] == ["chat"]
