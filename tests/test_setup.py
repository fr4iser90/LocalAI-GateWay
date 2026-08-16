"""First-run setup checklist + wizard helpers."""

from __future__ import annotations

from pathlib import Path

from app.admin.setup import needs_setup_wizard, setup_status, wizard_progress
from app.data.backends import upsert_source
from app.data.db import hash_api_key, hash_password
from app.data.models import (
    AdminUser,
    ApiKey,
    Base,
    CatalogModel,
    make_engine,
    make_session_factory,
)


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "setup.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_setup_checklist_and_wizard(tmp_path: Path):
    db = _session(tmp_path)
    admin = AdminUser(
        username="admin",
        password_hash=hash_password("x"),
        is_platform_admin=True,
        is_active=True,
    )
    db.add(admin)
    db.commit()

    assert needs_setup_wizard(db, admin) is True
    wiz = wizard_progress(db)
    assert wiz["complete"] is False
    assert wiz["next"]["id"] == "sources"

    upsert_source(db, name="chat", kind="chat", address="127.0.0.1:1", is_default=True)
    db.commit()
    wiz = wizard_progress(db)
    assert wiz["has_sources"] is True
    assert wiz["next"]["id"] == "models"

    db.add(CatalogModel(source_name="chat", kind="chat", model_id="m", enabled=True))
    db.commit()
    wiz = wizard_progress(db)
    assert wiz["next"]["id"] == "key"

    db.add(
        ApiKey(
            label="k",
            key_hash=hash_api_key("gw_x"),
            key_prefix="gw_x",
            owner_user_id=admin.id,
            is_active=True,
        )
    )
    db.commit()
    wiz = wizard_progress(db)
    assert wiz["complete"] is True
    assert needs_setup_wizard(db, admin) is False

    st = setup_status(db)
    assert st["complete"] is True
    assert st["steps"][0].done and st["steps"][1].done and st["steps"][2].done
