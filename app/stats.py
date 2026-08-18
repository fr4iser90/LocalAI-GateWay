"""Usage aggregations and readable SVG charts for dashboards."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from .data.models import UsageEvent, utcnow


def display_zone(name: str | None = None) -> ZoneInfo:
    """Resolve an IANA timezone name (invalid/empty → UTC)."""
    raw = (name or "").strip()
    if not raw:
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def is_valid_timezone(name: str) -> bool:
    raw = (name or "").strip()
    if not raw:
        return False
    try:
        ZoneInfo(raw)
        return True
    except ZoneInfoNotFoundError:
        return False


def zone_from_request(request=None, user=None) -> ZoneInfo:
    """Browser cookie gw_tz (auto-detected) → last saved user.timezone → UTC.

    No manual settings. Every logged-in UI user gets their browser zone.
    """
    raw = ""
    if request is not None:
        try:
            raw = (request.cookies.get("gw_tz") or "").strip()
        except Exception:
            raw = ""
    if not is_valid_timezone(raw):
        raw = ""
    if not raw and user is not None:
        raw = (getattr(user, "timezone", None) or "").strip()
    if not is_valid_timezone(raw):
        raw = ""
    return display_zone(raw or "UTC")


# Back-compat alias
def zone_for_user(user=None, *, fallback: str | None = None) -> ZoneInfo:
    if fallback and is_valid_timezone(fallback):
        if user is None or not (getattr(user, "timezone", None) or "").strip():
            return display_zone(fallback)
    return zone_from_request(None, user)

def week_window_start(tz: ZoneInfo | None = None):
    """Start of the oldest day in the 7-day chart (local midnight in display TZ)."""
    zone = tz or display_zone()
    today = utcnow().astimezone(zone).date()
    start = today - timedelta(days=6)
    return datetime(start.year, start.month, start.day, tzinfo=zone)


def _percentile(sorted_vals: list[float], p: float) -> float | None:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def _fmt_int(n: int | float) -> str:
    return f"{int(n):,}"


def usage_stats(
    db: Session,
    *,
    since=None,
    key_ids: list[int] | None = None,
    team_ids: set[int] | None = None,
    tz: ZoneInfo | None = None,
) -> dict:
    zone = tz or display_zone()
    since = since or (utcnow() - timedelta(days=1))
    q = db.query(UsageEvent).filter(UsageEvent.created_at >= since)
    if key_ids is not None:
        q = q.filter(UsageEvent.api_key_id.in_(key_ids)) if key_ids else q.filter(False)
    if team_ids is not None:
        q = q.filter(UsageEvent.team_id.in_(team_ids)) if team_ids else q.filter(False)

    events = q.all()
    ok = [e for e in events if e.result == "ok"]
    denies = sum(1 for e in events if e.result == "deny")
    rates = sum(1 for e in events if e.result == "rate_limit")

    by_service: dict[str, int] = {}
    by_model: dict[str, int] = {}
    top_keys: dict[str, int] = {}
    tokens_in = tokens_out = 0
    audio_seconds = 0.0
    response_chars = 0
    watt_hours = 0.0
    pool_cost = 0.0
    latencies: list[float] = []
    watts_samples: list[float] = []

    # Complete 7 calendar days ending today in display timezone
    today = utcnow().astimezone(zone).date()
    daily_map: dict[str, dict[str, float | int]] = {}
    day_labels: list[tuple[str, str]] = []  # (key, display)
    for i in range(6, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        day_labels.append((key, d.strftime("%a %d")))
        daily_map[key] = {"ok": 0, "deny": 0, "rate_limit": 0, "wh": 0.0}

    for e in events:
        if e.result == "ok":
            by_service[e.service] = by_service.get(e.service, 0) + 1
            if e.model:
                by_model[e.model] = by_model.get(e.model, 0) + 1
            top_keys[e.key_label or "(unknown)"] = top_keys.get(e.key_label or "(unknown)", 0) + 1
            tokens_in += e.tokens_in or 0
            tokens_out += e.tokens_out or 0
            audio_seconds += e.audio_seconds or 0.0
            response_chars += e.response_chars or 0
            watt_hours += float(getattr(e, "watt_hours", None) or 0)
            pool_cost += float(getattr(e, "pool_cost", None) or 0)
            if getattr(e, "watts", None) is not None:
                watts_samples.append(float(e.watts))
        if e.duration_ms is not None:
            latencies.append(float(e.duration_ms))
        if e.created_at:
            created = e.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            dkey = created.astimezone(zone).date().isoformat()
            if dkey in daily_map:
                bucket = e.result if e.result in ("ok", "deny", "rate_limit") else "deny"
                if bucket == "rate_limit":
                    daily_map[dkey]["rate_limit"] = int(daily_map[dkey]["rate_limit"]) + 1
                elif bucket == "ok":
                    daily_map[dkey]["ok"] = int(daily_map[dkey]["ok"]) + 1
                    daily_map[dkey]["wh"] = float(daily_map[dkey]["wh"]) + float(
                        getattr(e, "watt_hours", None) or 0
                    )
                else:
                    daily_map[dkey]["deny"] = int(daily_map[dkey]["deny"]) + 1

    latencies.sort()
    daily_series = [
        {
            "day": key,
            "label": label,
            "ok": int(daily_map[key]["ok"]),
            "deny": int(daily_map[key]["deny"]),
            "rate_limit": int(daily_map[key]["rate_limit"]),
            "watt_hours": round(float(daily_map[key]["wh"]), 4),
            "total": int(daily_map[key]["ok"])
            + int(daily_map[key]["deny"])
            + int(daily_map[key]["rate_limit"]),
        }
        for key, label in day_labels
    ]

    return {
        "total_ok": len(ok),
        "denies": denies,
        "rate_limits": rates,
        "demo_count": sum(1 for e in events if getattr(e, "is_demo", False)),
        "by_service": sorted(by_service.items(), key=lambda x: -x[1]),
        "by_model": sorted(by_model.items(), key=lambda x: -x[1])[:12],
        "top_keys": sorted(top_keys.items(), key=lambda x: -x[1])[:10],
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "audio_seconds": round(audio_seconds, 1),
        "response_chars": response_chars,
        "watt_hours": round(watt_hours, 4),
        "pool_cost": round(pool_cost, 2),
        "watts_avg": (sum(watts_samples) / len(watts_samples)) if watts_samples else None,
        "latency_p50": _percentile(latencies, 0.5),
        "latency_p95": _percentile(latencies, 0.95),
        "latency_avg": (sum(latencies) / len(latencies)) if latencies else None,
        "daily_series": daily_series,
        "event_count": len(events),
        "timezone": str(zone),
    }


def model_perf_averages(
    db: Session,
    *,
    key_ids: list[int] | None = None,
    lookback_days: int = 7,
    min_samples: int = 1,
) -> list[dict]:
    """Per-model averages from OK usage events (W, Wh, PP, TG, latency).

    ``key_ids=None`` → fleet (all keys). ``key_ids=[]`` → empty. Otherwise filter.
    """
    since = utcnow() - timedelta(days=max(1, int(lookback_days)))
    q = db.query(UsageEvent).filter(
        UsageEvent.created_at >= since,
        UsageEvent.result == "ok",
        UsageEvent.model.isnot(None),
        UsageEvent.model != "",
    )
    if key_ids is not None:
        if not key_ids:
            return []
        q = q.filter(UsageEvent.api_key_id.in_(key_ids))

    buckets: dict[str, dict[str, list[float]]] = {}
    counts: dict[str, int] = {}
    for e in q.all():
        model = (e.model or "").strip()
        if not model:
            continue
        counts[model] = counts.get(model, 0) + 1
        b = buckets.setdefault(
            model,
            {
                "watts": [],
                "watt_hours": [],
                "pp_tok_s": [],
                "tg_tok_s": [],
                "duration_ms": [],
            },
        )
        if e.watts is not None:
            b["watts"].append(float(e.watts))
        if e.watt_hours is not None and float(e.watt_hours) > 0:
            b["watt_hours"].append(float(e.watt_hours))
        if getattr(e, "pp_tok_s", None) is not None:
            b["pp_tok_s"].append(float(e.pp_tok_s))
        if getattr(e, "tg_tok_s", None) is not None:
            b["tg_tok_s"].append(float(e.tg_tok_s))
        if e.duration_ms is not None:
            b["duration_ms"].append(float(e.duration_ms))

    def _avg(vals: list[float]) -> float | None:
        return round(sum(vals) / len(vals), 3) if vals else None

    rows: list[dict] = []
    for model, n in counts.items():
        if n < min_samples:
            continue
        b = buckets[model]
        rows.append(
            {
                "model": model,
                "n": n,
                "watts_avg": _avg(b["watts"]),
                "watt_hours_avg": _avg(b["watt_hours"]),
                "pp_tok_s_avg": _avg(b["pp_tok_s"]),
                "tg_tok_s_avg": _avg(b["tg_tok_s"]),
                "duration_ms_avg": _avg(b["duration_ms"]),
            }
        )
    rows.sort(key=lambda r: (-r["n"], r["model"]))
    return rows


def model_perf_by_id(
    rows: list[dict],
) -> dict[str, dict]:
    """Index averages by model_id for catalog templates."""
    return {str(r["model"]): r for r in rows}


def bar_chart_svg(
    rows: list[tuple[str, float | int]],
    *,
    width: int = 560,
    unit: str = "requests",
) -> str:
    """Horizontal bar chart with axis max and value labels."""
    if not rows:
        return '<div class="chart-empty">No data in this window.</div>'
    max_v = max(float(v) for _, v in rows) or 1.0
    row_h = 28
    left = 132
    right = 56
    top = 8
    plot_w = width - left - right
    height = top + row_h * len(rows) + 28
    lines = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="Bar chart of {escape(unit)}">'
        f'<text x="{left}" y="14" class="chart-axis-title">{escape(unit)} · max {_fmt_int(max_v)}</text>'
    ]
    for i, (label, val) in enumerate(rows):
        y = top + 18 + i * row_h
        w = max(4, int((float(val) / max_v) * plot_w))
        safe = escape(str(label)[:26])
        lines.append(
            f'<text x="{left - 8}" y="{y + 12}" text-anchor="end" class="chart-label">{safe}</text>'
            f'<rect x="{left}" y="{y}" width="{plot_w}" height="16" rx="3" class="chart-track"/>'
            f'<rect x="{left}" y="{y}" width="{w}" height="16" rx="3" class="chart-bar"/>'
            f'<text x="{left + w + 6}" y="{y + 12}" class="chart-val">{_fmt_int(val)}</text>'
        )
    lines.append("</svg>")
    return "\n".join(lines)


def _nice_ceiling(v: int) -> int:
    """Round an axis max so ticks land on even steps (5 → 6, 53 → 60)."""
    v = max(1, int(v))
    if v <= 4:
        return v
    if v <= 6:
        return 6
    if v <= 8:
        return 8
    if v <= 10:
        return 10
    if v <= 50:
        step = 5
    elif v <= 100:
        step = 10
    elif v <= 250:
        step = 25
    else:
        step = 50
    return ((v + step - 1) // step) * step


def _axis_ticks(max_v: int) -> list[int]:
    if max_v <= 4:
        return list(range(0, max_v + 1))
    for n in (4, 3, 5, 2):
        if max_v % n == 0:
            step = max_v // n
            return [i * step for i in range(n + 1)]
    return [0, max_v // 2, max_v]


# Plot-only SVG: legend lives in HTML chrome so labels never collide with it.
_CHART_LEFT = 40
_CHART_RIGHT = 12
_CHART_TOP = 24
_CHART_PLOT_H = 156
_CHART_BOTTOM = 28
_CHART_H = _CHART_TOP + _CHART_PLOT_H + _CHART_BOTTOM


def _traffic_axes(left: float, top: float, plot_w: float, plot_h: float, max_v: int) -> list[str]:
    out: list[str] = []
    for t in _axis_ticks(max_v):
        y = top + plot_h - (t / max_v) * plot_h
        grid = "chart-grid chart-grid--base" if t == 0 else "chart-grid"
        out.append(
            f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_w}" y2="{y:.1f}" class="{grid}"/>'
            f'<text x="{left - 8}" y="{y:.1f}" text-anchor="end" dominant-baseline="central" '
            f'class="chart-axis">{_fmt_int(t)}</text>'
        )
    return out


def _svg_bars(series: list[dict], *, width: int, max_v: int) -> str:
    n = len(series)
    left, top, plot_h = _CHART_LEFT, _CHART_TOP, _CHART_PLOT_H
    plot_w = width - left - _CHART_RIGHT
    slot = plot_w / n
    bar_w = min(44.0, max(16.0, slot * 0.48))
    lines = [
        f'<svg class="chart chart-daily" viewBox="0 0 {width} {_CHART_H}" role="img" '
        f'aria-label="Stacked requests per day">'
    ]
    lines.extend(_traffic_axes(left, top, plot_w, plot_h, max_v))
    for i, s in enumerate(series):
        x = left + i * slot + (slot - bar_w) / 2
        cx = x + bar_w / 2
        y_cursor = top + plot_h
        for cls, val in (("seg-ok", s["ok"]), ("seg-deny", s["deny"]), ("seg-rate", s["rate_limit"])):
            if val <= 0:
                continue
            h = (val / max_v) * plot_h
            y_cursor -= h
            lines.append(
                f'<rect x="{x:.1f}" y="{y_cursor:.1f}" width="{bar_w:.1f}" height="{h:.1f}" '
                f'class="{cls}"/>'
            )
        total_cls = "chart-total" if s["total"] else "chart-total is-zero"
        lines.append(
            f'<text x="{cx:.1f}" y="{top - 8}" text-anchor="middle" class="{total_cls}">'
            f'{_fmt_int(s["total"])}</text>'
        )
        lines.append(
            f'<text x="{cx:.1f}" y="{top + plot_h + 18}" text-anchor="middle" '
            f'class="chart-label">{escape(s["label"])}</text>'
        )
    lines.append("</svg>")
    return "".join(lines)


def daily_traffic_chart_svg(
    series: list[dict], *, width: int = 720, tz_label: str = "UTC"
) -> str:
    """7-day stacked OK / deny / rate-limit bars."""
    if not series:
        return '<div class="chart-empty">No data in this window.</div>'

    total_all = sum(int(s.get("total") or 0) for s in series)
    if total_all <= 0:
        tz_safe = escape(tz_label or "UTC")
        return (
            '<div class="chart-empty chart-empty--soft">'
            f"<strong>No traffic yet</strong>"
            f"<span>Last 7 days ({tz_safe}) — counts appear after the first request.</span>"
            "</div>"
        )

    raw_max = max(int(s["total"]) for s in series) or 1
    max_v = _nice_ceiling(raw_max)
    svg = _svg_bars(series, width=width, max_v=max_v)
    rows_html = "".join(
        f"<tr><td>{escape(s['label'])}</td>"
        f"<td class='num'>{s['ok']}</td>"
        f"<td class='num'>{s['deny']}</td>"
        f"<td class='num'>{s['rate_limit']}</td>"
        f"<td class='num'><strong>{s['total']}</strong></td></tr>"
        for s in series
    )
    table = (
        '<details class="chart-details">'
        "<summary>Exact daily counts</summary>"
        '<table class="chart-table"><thead><tr>'
        "<th>Day</th><th>OK</th><th>Deny</th><th>429</th><th>Total</th>"
        f"</tr></thead><tbody>{rows_html}</tbody></table></details>"
    )
    return (
        '<div class="chart-frame">'
        '<div class="chart-meta">'
        '<div class="chart-legend" role="list">'
        '<span class="chart-legend-item" role="listitem">'
        '<span class="chart-swatch chart-swatch--ok" aria-hidden="true"></span>OK</span>'
        '<span class="chart-legend-item" role="listitem">'
        '<span class="chart-swatch chart-swatch--deny" aria-hidden="true"></span>Deny</span>'
        '<span class="chart-legend-item" role="listitem">'
        '<span class="chart-swatch chart-swatch--rate" aria-hidden="true"></span>Rate limit</span>'
        "</div>"
        '<span class="chart-unit">requests / day</span>'
        "</div>"
        f"{svg}"
        f"{table}"
        "</div>"
    )
