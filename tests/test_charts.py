from __future__ import annotations

from app.stats import _axis_ticks, _nice_ceiling, daily_traffic_chart_svg


def test_nice_ceiling_rounds_readable():
    assert _nice_ceiling(53) == 60
    assert _nice_ceiling(26) == 30
    assert _nice_ceiling(5) == 6


def test_axis_ticks_are_even():
    assert _axis_ticks(6) == [0, 2, 4, 6]
    assert _axis_ticks(4) == [0, 1, 2, 3, 4]


def test_daily_chart_is_stacked_bars_with_html_legend():
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
    assert 'class="chart-frame"' in html
    assert 'class="chart-legend"' in html
    assert "Rate limit" in html
    assert "requests / day" in html
    assert "Stacked requests per day" in html
    assert "seg-ok" in html
    assert "seg-deny" in html
    assert "seg-rate" in html
    assert "Exact daily counts" in html
    assert "data-chart-switch" not in html
    assert 'data-chart-tab="area"' not in html
    assert "chart-area-line" not in html


def test_daily_chart_keeps_labels_out_of_legend():
    series = [{"label": "Mon 1", "ok": 4, "deny": 0, "rate_limit": 0, "total": 4}]
    html = daily_traffic_chart_svg(series, tz_label="UTC")
    assert 'viewBox="0 0 720 208"' in html
    # Legend is HTML chrome, not drawn inside the SVG plot.
    svg = html[html.index("<svg") : html.index("</svg>")]
    assert "OK</text>" not in svg
    assert "Rate limit" not in svg
    assert "requests / day" not in svg
    assert 'class="chart-total"' in svg
    assert 'y="16"' in svg  # totals sit in the 24px lane above the plot (top-8)
