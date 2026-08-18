"""Unit tests for source-sidecar sysfs readers (no GPU required)."""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_sidecar():
    root = Path(__file__).resolve().parents[1]
    path = root / "services" / "source-sidecar" / "main.py"
    spec = importlib.util.spec_from_file_location("source_sidecar_main", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_sysfs_power(tmp_path: Path, monkeypatch):
    sidecar = _load_sidecar()
    root = tmp_path / "sys"
    hw = root / "class" / "drm" / "card0" / "device" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "power1_average").write_text("150000000\n", encoding="utf-8")
    monkeypatch.setattr(sidecar.power, "SYS_ROOT", root)
    watts, device = sidecar._scan_sysfs_power()
    assert watts == 150.0
    assert "card0" in device
    payload = sidecar.read_power()
    assert payload["ok"] is True
    assert payload["watts"] == 150.0


def test_read_temperature_prefers_named_hwmon_sensor(tmp_path: Path, monkeypatch):
    sidecar = _load_sidecar()
    root = tmp_path / "sys"
    hw = root / "class" / "hwmon" / "hwmon0"
    hw.mkdir(parents=True)
    (hw / "name").write_text("amdgpu\n", encoding="utf-8")
    (hw / "temp1_label").write_text("edge\n", encoding="utf-8")
    (hw / "temp1_input").write_text("41000\n", encoding="utf-8")
    monkeypatch.setattr(sidecar.thermal, "SYS_ROOT", root)
    monkeypatch.setattr(sidecar.thermal, "THERMAL_ROOT", root / "class" / "thermal")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_NAME", "amdgpu")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_LABEL", "edge")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_PATH", "")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_TYPE", "")
    monkeypatch.setattr(sidecar.thermal, "TEMP_MAX_C", 85.0)
    payload = sidecar.read_temperature()
    assert payload["ok"] is True
    assert payload["temperature_c"] == 41.0
    assert payload["sensor_name"] == "amdgpu"
    assert payload["sensor_label"] == "edge"
    assert payload["limit_c"] == 85.0


def test_temp_check_falls_back_to_thermal_zone(tmp_path: Path, monkeypatch):
    sidecar = _load_sidecar()
    root = tmp_path / "sys"
    thermal = root / "class" / "thermal" / "thermal_zone0"
    thermal.mkdir(parents=True)
    (thermal / "type").write_text("acpitz\n", encoding="utf-8")
    (thermal / "temp").write_text("43000\n", encoding="utf-8")
    monkeypatch.setattr(sidecar.thermal, "SYS_ROOT", root)
    monkeypatch.setattr(sidecar.thermal, "THERMAL_ROOT", root / "class" / "thermal")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_NAME", "")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_LABEL", "")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_PATH", "")
    monkeypatch.setattr(sidecar.thermal, "TEMP_SENSOR_TYPE", "acpitz")
    payload = sidecar.read_temperature()
    assert payload["ok"] is True
    assert payload["temperature_c"] == 43.0
    assert payload["sensor_name"] == "acpitz"
