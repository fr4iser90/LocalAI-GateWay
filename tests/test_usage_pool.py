"""Usage pool cost + window reset."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from app.data.models import (
    WebUser,
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
        pool_tokens_per_unit=1,
        pool_min_cost=1.0,
    )
    db.add(auth)
    owner = WebUser(
        username="friend",
        password_hash="x",
        is_platform_admin=False,
        pool_limit=75,
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
    # ~15 estimated tokens each (min_cost 1 does not apply as floor above tokens)
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
    assert owner.pool_used <= 75


def test_model_weight_lookup(tmp_path: Path):
    db = _session(tmp_path)
    auth = AuthSettings(pool_model_weights_enabled=False)
    db.add(auth)
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
    # Default: weights disabled → always 1.0 even if catalog has 4.5
    assert model_usage_weight(db, service="chat", model="heavy", auth=auth) == 1.0
    auth.pool_model_weights_enabled = True
    assert model_usage_weight(db, service="chat", model="heavy", auth=auth) == 4.5
    assert model_usage_weight(db, service="chat", model="missing", auth=auth) == 1.0
    assert model_usage_weight(db, service="chat", model="heavy", auth=None) == 1.0

def test_probe_url_from_source_address():
    from types import SimpleNamespace

    from app.usage_pool import (
        probe_url_for_source,
        suggest_gpu_power_url,
        suggest_temp_guard_url,
        temp_guard_url_for_source,
    )

    assert suggest_gpu_power_url("192.168.1.10:11535") == "http://192.168.1.10:9105/power"
    assert suggest_temp_guard_url("192.168.1.10:11535") == "http://192.168.1.10:9105/check"
    assert suggest_gpu_power_url("") == ""
    assert suggest_temp_guard_url("") == ""
    assert (
        probe_url_for_source(SimpleNamespace(address="10.0.0.5:8080"))
        == "http://10.0.0.5:9105/power"
    )
    assert (
        temp_guard_url_for_source(
            SimpleNamespace(address="10.0.0.5:8080", temp_guard_enabled=True)
        )
        == "http://10.0.0.5:9105/check"
    )
    # stored override ignored — sidecar is always co-located with source
    assert (
        probe_url_for_source(
            SimpleNamespace(gpu_power_url="http://elsewhere:9105/power", address="10.0.0.5:1")
        )
        == "http://10.0.0.5:9105/power"
    )
    assert (
        temp_guard_url_for_source(
            SimpleNamespace(address="10.0.0.5:1", temp_guard_enabled=False)
        )
        == ""
    )
    assert probe_url_for_source(None) == ""
    assert temp_guard_url_for_source(None) == ""
    assert probe_url_for_source(SimpleNamespace(address="")) == ""


def test_migrate_pool_to_token_budget(tmp_path: Path):
    from app.usage_pool import migrate_pool_to_token_budget

    db = _session(tmp_path)
    auth = AuthSettings(pool_tokens_per_unit=1000, pool_min_cost=1.0)
    db.add(auth)
    u = WebUser(
        username="u",
        password_hash="x",
        pool_limit=100,
        pool_used=2.5,
    )
    db.add(u)
    db.commit()

    assert migrate_pool_to_token_budget(db, auth) is True
    assert auth.pool_tokens_per_unit == 1
    assert u.pool_limit == 100_000
    assert u.pool_used == 2500.0
    assert migrate_pool_to_token_budget(db, auth) is False


def test_observed_tokens_per_sec(tmp_path: Path):
    from app.data.models import UsageEvent
    from app.usage_pool import observed_tokens_per_sec, resolve_tokens_per_sec

    db = _session(tmp_path)
    assert observed_tokens_per_sec(db) is None
    assert resolve_tokens_per_sec(db) == 50.0

    for _ in range(5):
        db.add(
            UsageEvent(
                key_label="k",
                team_name="",
                service="chat",
                method="POST",
                path="/v1/chat/completions",
                host="h",
                client_ip="1.1.1.1",
                status=200,
                result="ok",
                duration_ms=1000.0,
                tokens_in=40,
                tokens_out=60,
            )
        )
    db.commit()
    assert observed_tokens_per_sec(db) == 100.0
    assert resolve_tokens_per_sec(db) == 100.0
