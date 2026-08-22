"""Routing strategy helpers."""

from app.data.models import ApiKey, Team
from app.data.routing_strategy import (
    effective_preferred_source,
    effective_routing_for_key,
    effective_routing_strategy,
    normalize_routing_strategy,
)


def test_normalize_defaults():
    assert normalize_routing_strategy(None) == "load_aware"
    assert normalize_routing_strategy("round_robin") == "round_robin"
    assert normalize_routing_strategy("bogus") == "load_aware"


class _Auth:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def test_effective_platform_strategy():
    auth = _Auth(routing_strategy="round_robin", load_aware_routing=True)
    assert effective_routing_strategy(auth) == "round_robin"


def test_effective_team_overrides_platform():
    auth = _Auth(routing_strategy="load_aware")
    team = Team(name="ops", routing_strategy="round_robin")
    assert effective_routing_strategy(auth, team=team) == "round_robin"


def test_effective_key_overrides_team():
    auth = _Auth(routing_strategy="load_aware")
    team = Team(name="ops", routing_strategy="round_robin")
    key = ApiKey(label="k", key_hash="x", routing_strategy="name")
    assert effective_routing_strategy(auth, team=team, api_key=key) == "name"


def test_effective_preferred_source_chain():
    team = Team(name="ops", preferred_source="gpu-a")
    key = ApiKey(label="k", key_hash="x", preferred_source="gpu-b")
    assert effective_preferred_source(team=team) == "gpu-a"
    assert effective_preferred_source(team=team, api_key=key) == "gpu-b"


def test_effective_routing_for_key_tuple():
    auth = _Auth(routing_strategy="load_aware")
    team = Team(name="dev", routing_strategy="round_robin", preferred_source="")
    key = ApiKey(label="k", key_hash="x", team=team)
    strat, pref = effective_routing_for_key(auth, key)
    assert strat == "round_robin"
    assert pref is None
