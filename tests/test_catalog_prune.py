"""Catalog sync: auto-disable stale rows when upstream no longer lists them."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from app.data.backends import upsert_source
from app.data.catalog import (
    DISABLED_BY_ADMIN,
    DISABLED_BY_SYNC,
    DiscoveredModel,
    SourceDiscovery,
    sync_catalog_from_sources,
)
from app.data.models import AuthSettings, Base, CatalogModel, make_engine, make_session_factory


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "prune.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def _seed_chat_source(db):
    upsert_source(db, name="chat", kind="chat", address="10.0.0.1:1", is_default=True)
    db.commit()


def test_sync_prunes_stale_models(tmp_path: Path):
    db = _session(tmp_path)
    _seed_chat_source(db)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="old-model",
            enabled=True,
        )
    )
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="current",
            enabled=True,
        )
    )
    db.commit()

    def fake_discover(address: str, kind: str) -> SourceDiscovery:
        return SourceDiscovery([DiscoveredModel(model_id="current")], ok=True)

    with patch("app.data.catalog.discover_models_for_source", fake_discover):
        stats = sync_catalog_from_sources(db)
        db.commit()

    assert stats["pruned"] == 1
    old = db.query(CatalogModel).filter(CatalogModel.model_id == "old-model").one()
    assert old.enabled is False
    assert old.disabled_by == DISABLED_BY_SYNC
    current = db.query(CatalogModel).filter(CatalogModel.model_id == "current").one()
    assert current.enabled is True
    assert (current.disabled_by or "") == ""


def test_sync_no_prune_when_upstream_failed(tmp_path: Path):
    db = _session(tmp_path)
    _seed_chat_source(db)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="orphan",
            enabled=True,
        )
    )
    db.commit()

    def fake_discover(address: str, kind: str) -> SourceDiscovery:
        return SourceDiscovery([], ok=False)

    with patch("app.data.catalog.discover_models_for_source", fake_discover):
        stats = sync_catalog_from_sources(db)
        db.commit()

    assert stats["pruned"] == 0
    row = db.query(CatalogModel).filter(CatalogModel.model_id == "orphan").one()
    assert row.enabled is True


def test_sync_leaves_admin_disabled_alone(tmp_path: Path):
    db = _session(tmp_path)
    _seed_chat_source(db)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="admin-off",
            enabled=False,
            disabled_by=DISABLED_BY_ADMIN,
        )
    )
    db.commit()

    def fake_discover(address: str, kind: str) -> SourceDiscovery:
        return SourceDiscovery([DiscoveredModel(model_id="live")], ok=True)

    with patch("app.data.catalog.discover_models_for_source", fake_discover):
        stats = sync_catalog_from_sources(db)
        db.commit()

    assert stats["pruned"] == 0
    row = db.query(CatalogModel).filter(CatalogModel.model_id == "admin-off").one()
    assert row.enabled is False
    assert row.disabled_by == DISABLED_BY_ADMIN


def test_sync_reenables_when_model_returns(tmp_path: Path):
    db = _session(tmp_path)
    _seed_chat_source(db)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="returned",
            enabled=False,
            disabled_by=DISABLED_BY_SYNC,
        )
    )
    db.commit()

    def fake_discover(address: str, kind: str) -> SourceDiscovery:
        return SourceDiscovery([DiscoveredModel(model_id="returned")], ok=True)

    with patch("app.data.catalog.discover_models_for_source", fake_discover):
        sync_catalog_from_sources(db)
        db.commit()

    row = db.query(CatalogModel).filter(CatalogModel.model_id == "returned").one()
    assert row.enabled is True
    assert (row.disabled_by or "") == ""


def test_sync_prune_respects_setting(tmp_path: Path):
    db = _session(tmp_path)
    _seed_chat_source(db)
    db.add(AuthSettings(catalog_prune_on_sync=False))
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="stale",
            enabled=True,
        )
    )
    db.commit()

    def fake_discover(address: str, kind: str) -> SourceDiscovery:
        return SourceDiscovery([DiscoveredModel(model_id="live")], ok=True)

    with patch("app.data.catalog.discover_models_for_source", fake_discover):
        stats = sync_catalog_from_sources(db)
        db.commit()

    assert stats["pruned"] == 0
    row = db.query(CatalogModel).filter(CatalogModel.model_id == "stale").one()
    assert row.enabled is True


def test_split_sync_stale_pairs():
    from app.data.catalog import is_sync_stale_pair, split_sync_stale_pairs

    live = CatalogModel(
        source_name="chat", kind="chat", model_id="live", enabled=True, disabled_by=""
    )
    sync_off = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="gone",
        enabled=False,
        disabled_by=DISABLED_BY_SYNC,
    )
    admin_off = CatalogModel(
        source_name="chat",
        kind="chat",
        model_id="manual",
        enabled=False,
        disabled_by=DISABLED_BY_ADMIN,
    )
    assert is_sync_stale_pair(sync_off, None)
    assert not is_sync_stale_pair(live, None)
    assert not is_sync_stale_pair(admin_off, None)
    assert not is_sync_stale_pair(sync_off, live)

    active, stale = split_sync_stale_pairs(
        [(live, None), (sync_off, None), (admin_off, None)]
    )
    assert [p[0].model_id for p in active] == ["live", "manual"]
    assert [p[0].model_id for p in stale] == ["gone"]
