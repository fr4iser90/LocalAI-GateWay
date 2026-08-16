"""Unit tests for gpu-power sysfs reader (no GPU required)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_gp():
    root = Path(__file__).resolve().parents[1]
    path = root / "services" / "gpu-power" / "main.py"
    spec = importlib.util.spec_from_file_location("gpu_power_main", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_sysfs_power(tmp_path: Path, monkeypatch):
    gp = _load_gp()
    root = tmp_path / "sys"
    hw = root / "class" / "drm" / "card0" / "device" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "power1_average").write_text("150000000\n")
    monkeypatch.setattr(gp, "SYS_ROOT", root)
    watts, device = gp._scan_sysfs()
    assert watts == 150.0
    assert "card0" in device
    payload = gp.read_power()
    assert payload["ok"] is True
    assert payload["watts"] == 150.0
