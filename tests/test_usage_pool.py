"""Usage pool cost + window reset."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.data.models import (
    AdminUser,
    AuthSettings,
    Base,
    CatalogModel,
    make_engine,
    make_session_factory,
    utcnow,
)
from app.usage_pool import (
    check_and_consume_pool,
    compute_cost,
    model_usage_weight,
)
from app.data.db import hash_api_key
from app.data.models import ApiKey


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "pool.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_compute_cost_tokens_and_weight():
    # 2000 tokens / 1000 = 2, × weight 3 = 6
    assert compute_cost(tokens=2000, weight=3.0, tokens_per_unit=1000, min_cost=1.0) == 6.0
    # tiny prompt still pays min_cost × weight
    assert compute_cost(tokens=10, weight=2.0, tokens_per_unit=1000, min_cost=1.0) == 2.0


def test_compute_cost_watt_hook():
    c = compute_cost(
        tokens=0,
        weight=1.0,
        tokens_per_unit=1000,
        min_cost=1.0,
        watt_hours=0.5,
        watt_weight=2.0,
    )
    assert c == 2.0  # min 1 + 0.5*2


def test_estimate_watt_hours():
    from app.usage_pool import estimate_watt_hours

    # 360 W for 10 s → 360*10/3600 = 1 Wh
    wh = estimate_watt_hours(watts=360.0, tokens=500, tokens_per_sec=50.0)
    assert wh == 1.0
    assert estimate_watt_hours(watts=None, tokens=100, tokens_per_sec=50) is None


def test_watt_hours_from_samples():
    from app.usage_pool import watt_hours_from_samples

    avg, wh = watt_hours_from_samples(samples=[100.0, 200.0], duration_sec=36.0)
    assert avg == 150.0
    assert wh == 1.5
    assert watt_hours_from_samples(samples=[], duration_sec=10) == (None, None)


def test_pool_exhaust_and_window(tmp_path: Path):
    db = _session(tmp_path)
    auth = AuthSettings(
        pool_window_hours=5,
        pool_tokens_per_unit=1000,
        pool_min_cost=1.0,
    )
    db.add(auth)
    owner = AdminUser(
        username="friend",
        password_hash="x",
        is_platform_admin=False,
        pool_limit=5,
        pool_used=0.0,
    )
    db.add(owner)
    db.flush()
    key = ApiKey(
        label="k",
        key_hash=hash_api_key("gw_x"),
        key_prefix="gw_x",
        owner_user_id=owner.id,
        is_active=True,
    )
    db.add(key)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="Qwen",
            enabled=True,
            usage_weight=1.0,
        )
    )
    db.commit()
    db.refresh(key)
    key.owner = owner

    body = b'{"model":"Qwen","messages":[{"role":"user","content":"hi"}]}'
    # tiny prompt → min_cost 1.0 each
    for _ in range(5):
        d = check_and_consume_pool(
            db, api_key=key, auth=auth, service="chat", model="Qwen", body=body
        )
        assert d.allowed, d
    d = check_and_consume_pool(
        db, api_key=key, auth=auth, service="chat", model="Qwen", body=body
    )
    assert not d.allowed
    assert d.reason == "usage_pool_exhausted"

    # force window expired
    owner.pool_window_start = utcnow() - timedelta(hours=6)
    db.flush()
    d = check_and_consume_pool(
        db, api_key=key, auth=auth, service="chat", model="Qwen", body=body
    )
    assert d.allowed
    assert owner.pool_used < 5


def test_model_weight_lookup(tmp_path: Path):
    db = _session(tmp_path)
    db.add(
        CatalogModel(
            source_name="chat",
            kind="chat",
            model_id="heavy",
            enabled=True,
            usage_weight=4.5,
        )
    )
    db.commit()
    assert model_usage_weight(db, service="chat", model="heavy") == 4.5
    assert model_usage_weight(db, service="chat", model="missing") == 1.0


def test_probe_url_from_source_address():
    from types import SimpleNamespace

    from app.usage_pool import probe_url_for_source, suggest_gpu_power_url

    assert suggest_gpu_power_url("192.168.1.10:11535") == "http://192.168.1.10:9105/power"
    assert suggest_gpu_power_url("") == ""
    assert (
        probe_url_for_source(SimpleNamespace(address="10.0.0.5:8080"))
        == "http://10.0.0.5:9105/power"
    )
    # stored override ignored — sidecar is always co-located with source
    assert (
        probe_url_for_source(
            SimpleNamespace(gpu_power_url="http://elsewhere:9105/power", address="10.0.0.5:1")
        )
        == "http://10.0.0.5:9105/power"
    )
    assert probe_url_for_source(None) == ""
    assert probe_url_for_source(SimpleNamespace(address="")) == ""
