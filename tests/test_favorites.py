"""Favorites ordering for GET /v1/models."""

from __future__ import annotations

from pathlib import Path

from app.data.backends import upsert_source
from app.data.catalog import (
    favorites_for_key,
    models_visible_for_key,
    openai_models_payload,
)
from app.data.db import hash_api_key
from app.data.models import (
    ApiKey,
    Base,
    CatalogModel,
    ModelFavorite,
    ServiceGrant,
    Team,
    make_engine,
    make_session_factory,
)
from sqlalchemy.orm import joinedload


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "f.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_favorites_sort_models_first(tmp_path: Path):
    db = _session(tmp_path)
    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    for mid in ("alpha", "beta", "gamma"):
        db.add(CatalogModel(source_name="chat", kind="chat", model_id=mid, enabled=True))
    key = ApiKey(
        label="t",
        key_hash=hash_api_key("gw_fav"),
        key_prefix="gw_fav",
        is_active=True,
    )
    db.add(key)
    db.flush()
    db.add(ServiceGrant(api_key_id=key.id, service="chat"))
    db.add(
        ModelFavorite(
            api_key_id=key.id, service="chat", model_name="gamma", sort_order=0
        )
    )
    db.add(
        ModelFavorite(
            api_key_id=key.id, service="chat", model_name="alpha", sort_order=1
        )
    )
    db.commit()

    rows = models_visible_for_key(db, key)
    payload = openai_models_payload(rows, favorites_for_key(key))
    assert [x["id"] for x in payload["data"]] == ["gamma", "alpha", "beta"]


def test_key_favorites_override_team(tmp_path: Path):
    db = _session(tmp_path)
    team = Team(name="t1")
    db.add(team)
    db.flush()
    db.add(
        ModelFavorite(
            team_id=team.id, service="chat", model_name="team-fav", sort_order=0
        )
    )
    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_t"),
        key_prefix="gw_t",
        is_active=True,
        team_id=team.id,
    )
    db.add(key)
    db.commit()
    key = (
        db.query(ApiKey)
        .options(
            joinedload(ApiKey.team).joinedload(Team.model_favorites),
            joinedload(ApiKey.model_favorites),
        )
        .filter(ApiKey.id == key.id)
        .one()
    )
    assert [f.model_name for f in favorites_for_key(key)] == ["team-fav"]
    db.add(
        ModelFavorite(
            api_key_id=key.id, service="chat", model_name="key-fav", sort_order=0
        )
    )
    db.commit()
    key = (
        db.query(ApiKey)
        .options(
            joinedload(ApiKey.team).joinedload(Team.model_favorites),
            joinedload(ApiKey.model_favorites),
        )
        .filter(ApiKey.id == key.id)
        .one()
    )
    assert [f.model_name for f in favorites_for_key(key)] == ["key-fav"]
