"""Unit tests for upstream usage/timings parse + per-model averages."""

from __future__ import annotations

import json
from pathlib import Path

from app.audit import bump_usage_daily, finalize_usage_metering
from app.data.models import (
    ApiKey,
    Base,
    UsageDaily,
    UsageEvent,
    make_engine,
    make_session_factory,
    utcnow,
)
from app.metering_parse import parse_upstream_metrics
from app.stats import model_perf_averages, model_perf_by_id


def _session(tmp_path: Path):
    eng = make_engine(str(tmp_path / "meter.db"))
    Base.metadata.create_all(bind=eng)
    return make_session_factory(eng)()


def test_parse_nonstream_usage_and_timings():
    payload = {
        "usage": {"prompt_tokens": 330, "completion_tokens": 11866, "total_tokens": 12196},
        "timings": {
            "prompt_per_second": 84.2117,
            "predicted_per_second": 22.3696,
        },
    }
    raw = json.dumps(payload).encode()
    m = parse_upstream_metrics(raw, duration_ms=578_000.0)
    assert m.tokens_in == 330
    assert m.tokens_out == 11866
    assert m.pp_tok_s is not None and abs(m.pp_tok_s - 84.2117) < 0.01
    assert m.tg_tok_s is not None and abs(m.tg_tok_s - 22.3696) < 0.01


def test_parse_tg_fallback_from_wall_duration():
    payload = {"usage": {"prompt_tokens": 10, "completion_tokens": 100}}
    m = parse_upstream_metrics(json.dumps(payload).encode(), duration_ms=2000.0)
    assert m.tokens_out == 100
    assert m.pp_tok_s is None
    assert m.tg_tok_s == 50.0  # 100 tokens / 2 s


def test_parse_sse_final_chunk():
    sse = (
        b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        b"data: "
        + json.dumps(
            {
                "usage": {"prompt_tokens": 5, "completion_tokens": 20},
                "timings": {
                    "prompt_per_second": 200.0,
                    "predicted_per_second": 40.0,
                },
            }
        ).encode()
        + b"\n\n"
        b"data: [DONE]\n\n"
    )
    m = parse_upstream_metrics(sse, duration_ms=500.0, content_type="text/event-stream")
    assert m.tokens_in == 5
    assert m.tokens_out == 20
    assert m.pp_tok_s == 200.0
    assert m.tg_tok_s == 40.0


def test_parse_integration_fixture_if_present():
    path = (
        Path(__file__).resolve().parents[1]
        / "output"
        / "integration"
        / "20260817T143157Z"
        / "chat_Qwen3.8-27B-Q4_K_M-MTP.json"
    )
    if not path.is_file():
        return
    m = parse_upstream_metrics(path.read_bytes(), duration_ms=578_000.0)
    assert m.tokens_in == 330
    assert m.tokens_out == 11866
    assert m.pp_tok_s and m.pp_tok_s > 50
    assert m.tg_tok_s and m.tg_tok_s > 10


def test_finalize_patches_tokens_and_throughput(tmp_path: Path):
    db = _session(tmp_path)
    key = ApiKey(
        label="k",
        key_prefix="gw_test",
        key_hash="x",
        is_active=True,
    )
    db.add(key)
    db.flush()
    ev = UsageEvent(
        api_key_id=key.id,
        service="chat",
        method="POST",
        path="/v1/chat/completions",
        host="localhost",
        client_ip="127.0.0.1",
        model="demo-model",
        status=204,
        result="ok",
        tokens_in=50,  # body estimate
        tokens_out=None,
    )
    db.add(ev)
    db.flush()
    bump_usage_daily(
        db,
        team_id=None,
        api_key_id=key.id,
        team_name="",
        key_label="k",
        service="chat",
        model="demo-model",
        result="ok",
        tokens_in=50,
        tokens_out=None,
    )
    db.commit()
    uid = ev.id

    finalize_usage_metering(
        db,
        uid,
        duration_ms=1000.0,
        watts=90.0,
        watt_hours=0.025,
        upstream_status=200,
        power_status="metered",
        tokens_in=40,
        tokens_out=80,
        pp_tok_s=120.5,
        tg_tok_s=80.0,
    )
    db.expire_all()
    ev2 = db.get(UsageEvent, uid)
    assert ev2 is not None
    assert ev2.tokens_in == 40
    assert ev2.tokens_out == 80
    assert ev2.pp_tok_s == 120.5
    assert ev2.tg_tok_s == 80.0
    assert ev2.watts == 90.0
    assert ev2.duration_ms == 1000.0

    daily = (
        db.query(UsageDaily)
        .filter(UsageDaily.api_key_id == key.id, UsageDaily.model == "demo-model")
        .one()
    )
    assert daily.tokens_in == 40
    assert daily.tokens_out == 80


def test_model_perf_averages_filters_by_key(tmp_path: Path):
    db = _session(tmp_path)
    k1 = ApiKey(label="a", key_prefix="gw_a", key_hash="ha", is_active=True)
    k2 = ApiKey(label="b", key_prefix="gw_b", key_hash="hb", is_active=True)
    db.add_all([k1, k2])
    db.flush()
    now = utcnow()
    for kid, tg in ((k1.id, 50.0), (k1.id, 70.0), (k2.id, 10.0)):
        db.add(
            UsageEvent(
                created_at=now,
                api_key_id=kid,
                service="chat",
                method="POST",
                path="/v1/chat/completions",
                host="h",
                client_ip="",
                model="M1",
                status=200,
                result="ok",
                duration_ms=1000.0,
                watts=100.0,
                watt_hours=0.01,
                pp_tok_s=200.0,
                tg_tok_s=tg,
            )
        )
    db.commit()

    mine = model_perf_averages(db, key_ids=[k1.id], lookback_days=7)
    assert len(mine) == 1
    assert mine[0]["model"] == "M1"
    assert mine[0]["n"] == 2
    assert mine[0]["tg_tok_s_avg"] == 60.0
    assert mine[0]["pp_tok_s_avg"] == 200.0

    fleet = model_perf_averages(db, key_ids=None, lookback_days=7)
    assert fleet[0]["n"] == 3
    assert abs(fleet[0]["tg_tok_s_avg"] - (50 + 70 + 10) / 3) < 0.01

    empty = model_perf_averages(db, key_ids=[], lookback_days=7)
    assert empty == []

    by_id = model_perf_by_id(fleet)
    assert "M1" in by_id
