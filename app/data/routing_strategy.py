"""Tie-break strategies when several sources serve the same catalog model."""

from __future__ import annotations

import threading

ROUTING_STRATEGIES: tuple[str, ...] = ("load_aware", "round_robin", "name")

ROUTING_STRATEGY_LABELS: dict[str, str] = {
    "load_aware": "Load-aware (least busy)",
    "round_robin": "Round-robin (rotate)",
    "name": "Stable (alphabetical)",
}


def normalize_routing_strategy(value: str | None) -> str:
    s = (value or "").strip().lower()
    if s in ROUTING_STRATEGIES:
        return s
    return "load_aware"


def routing_strategy_choices(*, include_inherit: bool = False) -> list[dict[str, str]]:
    rows = [
        {"id": sid, "label": ROUTING_STRATEGY_LABELS[sid], "summary": _summary(sid)}
        for sid in ROUTING_STRATEGIES
    ]
    if include_inherit:
        return [
            {
                "id": "",
                "label": "Inherit (platform default)",
                "summary": "Use tie-break from Platform → Settings → Routing.",
            },
            *rows,
        ]
    return rows


def effective_routing_strategy(auth, *, team=None, api_key=None) -> str:
    """Resolve tie-break strategy: key → team → platform."""
    if api_key is not None:
        key_strat = (getattr(api_key, "routing_strategy", None) or "").strip()
        if key_strat:
            return normalize_routing_strategy(key_strat)
    if team is not None:
        team_strat = (getattr(team, "routing_strategy", None) or "").strip()
        if team_strat:
            return normalize_routing_strategy(team_strat)
    if auth is None:
        return "load_aware"
    explicit = normalize_routing_strategy(getattr(auth, "routing_strategy", None))
    stored = (getattr(auth, "routing_strategy", None) or "").strip()
    if stored:
        return explicit
    if bool(getattr(auth, "load_aware_routing", True)):
        return "load_aware"
    return "name"


def effective_preferred_source(*, team=None, api_key=None) -> str | None:
    """Optional source pin: key → team → none."""
    if api_key is not None:
        key_pref = (getattr(api_key, "preferred_source", None) or "").strip().lower()
        if key_pref:
            return key_pref
    if team is not None:
        team_pref = (getattr(team, "preferred_source", None) or "").strip().lower()
        if team_pref:
            return team_pref
    return None


def effective_routing_for_key(auth, api_key) -> tuple[str, str | None]:
    team = getattr(api_key, "team", None) if api_key is not None else None
    if api_key is not None:
        key_strat = (getattr(api_key, "routing_strategy", None) or "").strip()
        if key_strat:
            strategy = normalize_routing_strategy(key_strat)
        elif team is not None and (getattr(team, "routing_strategy", None) or "").strip():
            strategy = normalize_routing_strategy(team.routing_strategy)
        elif auth is not None:
            strategy = effective_routing_strategy(auth)
        else:
            strategy = "load_aware"
    elif auth is not None:
        strategy = effective_routing_strategy(auth)
    else:
        strategy = "load_aware"
    return strategy, effective_preferred_source(team=team, api_key=api_key)


def _summary(strategy: str) -> str:
    if strategy == "load_aware":
        return "Pick source with most idle slots / model loaded (~3s probe cache)."
    if strategy == "round_robin":
        return "Rotate evenly across tied sources per model (in-memory)."
    return "Always pick the first source name (deterministic, no probes)."


class RoundRobinState:
    """In-process round-robin cursor per kind+model."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cursor: dict[str, int] = {}

    @staticmethod
    def _key(kind: str, model: str, source_names: list[str]) -> str:
        names = ",".join(sorted(source_names))
        return f"{kind}|{model.strip()}|{names}"

    def pick(self, *, kind: str, model: str, source_names: list[str]) -> str:
        if not source_names:
            return ""
        ordered = sorted(source_names)
        key = self._key(kind, model, ordered)
        with self._lock:
            idx = self._cursor.get(key, 0) % len(ordered)
            self._cursor[key] = idx + 1
            return ordered[idx]


round_robin_state = RoundRobinState()
