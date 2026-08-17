"""Optional budget-weight suggestions from metered usage (never auto-enforced)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy.orm import Session

from .catalog import list_catalog
from .models import UsageEvent, utcnow

MIN_SAMPLES_PER_MODEL = 3
MIN_MODELS_WITH_DATA = 2
LOOKBACK_DAYS = 7
WEIGHT_MIN = 0.5
WEIGHT_MAX = 5.0


@dataclass
class WeightSuggestion:
    catalog_id: int
    source_name: str
    model_id: str
    current_weight: float
    suggested_weight: float
    tg_tok_s_avg: float
    sample_count: int


@dataclass
class WeightSuggestionStatus:
    ready: bool
    message: str
    baseline_tg_tok_s: float | None = None
    model_count: int = 0
    suggestions: list[WeightSuggestion] = field(default_factory=list)


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _tg_rates_by_source_model(
    db: Session, *, lookback_days: int = LOOKBACK_DAYS
) -> dict[tuple[str, str], tuple[float, int]]:
    since = utcnow() - timedelta(days=max(1, lookback_days))
    buckets: dict[tuple[str, str], list[float]] = {}
    for e in (
        db.query(UsageEvent)
        .filter(
            UsageEvent.created_at >= since,
            UsageEvent.result == "ok",
            UsageEvent.model.isnot(None),
            UsageEvent.model != "",
            UsageEvent.tg_tok_s.isnot(None),
            UsageEvent.tg_tok_s > 0,
        )
        .all()
    ):
        svc = (e.service or "").strip()
        mid = (e.model or "").strip()
        if not svc or not mid:
            continue
        buckets.setdefault((svc, mid), []).append(float(e.tg_tok_s))
    out: dict[tuple[str, str], tuple[float, int]] = {}
    for key, vals in buckets.items():
        if len(vals) >= MIN_SAMPLES_PER_MODEL:
            out[key] = (sum(vals) / len(vals), len(vals))
    return out


def catalog_weight_suggestions(
    db: Session, *, lookback_days: int = LOOKBACK_DAYS
) -> WeightSuggestionStatus:
    rates = _tg_rates_by_source_model(db, lookback_days=lookback_days)
    if len(rates) < MIN_MODELS_WITH_DATA:
        return WeightSuggestionStatus(
            ready=False,
            message=(
                f"Need TG tok/s on at least {MIN_MODELS_WITH_DATA} models "
                f"({MIN_SAMPLES_PER_MODEL}+ OK calls each, last {lookback_days}d)."
            ),
            model_count=len(rates),
        )
    baseline = _median([avg for avg, _ in rates.values()])
    if baseline <= 0:
        return WeightSuggestionStatus(
            ready=False,
            message="Not enough throughput data yet.",
            model_count=len(rates),
        )

    suggestions: list[WeightSuggestion] = []
    for row in list_catalog(db):
        hit = rates.get((row.source_name, row.model_id))
        if not hit:
            continue
        tg_avg, n = hit
        raw = baseline / tg_avg
        suggested = round(max(WEIGHT_MIN, min(WEIGHT_MAX, raw)), 1)
        current = float(row.usage_weight or 1.0)
        suggestions.append(
            WeightSuggestion(
                catalog_id=row.id,
                source_name=row.source_name,
                model_id=row.model_id,
                current_weight=current,
                suggested_weight=suggested,
                tg_tok_s_avg=round(tg_avg, 1),
                sample_count=n,
            )
        )
    suggestions.sort(key=lambda s: (-s.suggested_weight, s.source_name, s.model_id))
    return WeightSuggestionStatus(
        ready=True,
        message=f"Baseline TG ~{baseline:.1f} tok/s from {len(rates)} model(s).",
        baseline_tg_tok_s=round(baseline, 1),
        model_count=len(rates),
        suggestions=suggestions,
    )


def apply_weight_suggestions(
    db: Session, suggestions: list[WeightSuggestion], *, min_delta: float = 0.05
) -> int:
    from .models import CatalogModel

    updated = 0
    for s in suggestions:
        if abs(s.suggested_weight - s.current_weight) < min_delta:
            continue
        row = db.get(CatalogModel, s.catalog_id)
        if row is None:
            continue
        row.usage_weight = s.suggested_weight
        updated += 1
    return updated
