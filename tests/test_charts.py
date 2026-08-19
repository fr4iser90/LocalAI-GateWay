from __future__ import annotations

from app.stats import _axis_ticks, _nice_ceiling, area_chart_svg, daily_traffic_chart_svg


def test_nice_ceiling_rounds_readable():
    assert _nice_ceiling(53) == 60
    assert _nice_ceiling(26) == 30
    assert _nice_ceiling(5) == 6


def test_axis_ticks_are_even():
    assert _axis_ticks(6) == [0, 2, 4, 6]
    assert _axis_ticks(4) == [0, 1, 2, 3, 4]


def test_daily_chart_is_area_line():
    series = [
        {
            "label": "Mon 1",
            "ok": 10,
            "deny": 1,
            "rate_limit": 2,
            "total": 13,
        }
    ]
    html = daily_traffic_chart_svg(series, tz_label="UTC")
    assert 'class="chart-area-line"' in html
    assert "gw-day" in html
    assert "Exact daily counts" in html
    assert "seg-ok" not in html
    assert "data-chart-switch" not in html
    assert "Stacked requests per day" not in html


def test_area_chart_has_no_point_labels():
    html = area_chart_svg([{"total": 4}, {"total": 2}], fill_id="t1", aria="Pulse")
    assert "chart-total" not in html
    assert "OK</text>" not in html
    assert 'class="chart-area-line"' in html
    assert "chart-area-stop-a" in html
