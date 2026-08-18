from __future__ import annotations

import os
from pathlib import Path

SYS_ROOT = Path(os.environ.get("SYS_ROOT", "/sys"))
THERMAL_ROOT = Path(os.environ.get("TEMP_THERMAL_ROOT", str(SYS_ROOT / "class" / "thermal")))

TEMP_SENSOR_NAME = os.environ.get("TEMP_SENSOR_NAME", "").strip()
TEMP_SENSOR_LABEL = os.environ.get("TEMP_SENSOR_LABEL", "").strip()
TEMP_SENSOR_PATH = os.environ.get("TEMP_SENSOR_PATH", "").strip()
TEMP_SENSOR_TYPE = os.environ.get("TEMP_SENSOR_TYPE", "").strip()
TEMP_MAX_C = float(os.environ.get("TEMP_MAX_C", "85"))


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def _read_temp_value(path: Path) -> float | None:
    try:
        raw = _read_text(path)
        return int(raw) / 1000.0
    except Exception:
        return None


def read_hwmon_temperatures() -> list[dict]:
    items: list[dict] = []
    hwmon = SYS_ROOT / "class" / "hwmon"
    if not hwmon.is_dir():
        return items
    for hw in sorted(hwmon.glob("hwmon*")):
        try:
            sensor_name = _read_text(hw / "name")
        except Exception:
            sensor_name = hw.name
        for temp_path in sorted(hw.glob("temp*_input")):
            base = temp_path.name[: -len("_input")]
            temp_c = _read_temp_value(temp_path)
            if temp_c is None:
                continue
            label_path = hw / f"{base}_label"
            label = _read_text(label_path) if label_path.is_file() else ""
            items.append(
                {
                    "kind": "hwmon",
                    "name": sensor_name,
                    "label": label,
                    "path": str(temp_path),
                    "c": round(temp_c, 2),
                }
            )
    return items


def read_thermal_zones() -> list[dict]:
    items: list[dict] = []
    if not THERMAL_ROOT.is_dir():
        return items
    for zone in sorted(THERMAL_ROOT.glob("thermal_zone*")):
        temp_path = zone / "temp"
        type_path = zone / "type"
        if not temp_path.is_file():
            continue
        temp_c = _read_temp_value(temp_path)
        if temp_c is None:
            continue
        sensor_type = _read_text(type_path) if type_path.is_file() else zone.name
        items.append(
            {
                "kind": "thermal_zone",
                "name": sensor_type,
                "label": "",
                "path": str(temp_path),
                "c": round(temp_c, 2),
            }
        )
    return items


def _match_temperature(items: list[dict]) -> dict | None:
    if TEMP_SENSOR_PATH:
        wanted = TEMP_SENSOR_PATH.strip()
        for item in items:
            if item["path"] == wanted:
                return item
        temp_c = _read_temp_value(Path(wanted))
        if temp_c is not None:
            return {
                "kind": "explicit_path",
                "name": TEMP_SENSOR_NAME or "custom",
                "label": TEMP_SENSOR_LABEL,
                "path": wanted,
                "c": round(temp_c, 2),
            }

    if TEMP_SENSOR_NAME:
        named = [item for item in items if item["name"] == TEMP_SENSOR_NAME]
        if TEMP_SENSOR_LABEL:
            for item in named:
                if item["label"] == TEMP_SENSOR_LABEL:
                    return item
        if named:
            return max(named, key=lambda item: float(item["c"]))

    if TEMP_SENSOR_TYPE:
        for item in items:
            if item["kind"] == "thermal_zone" and item["name"] == TEMP_SENSOR_TYPE:
                return item

    preferred = [
        ("amdgpu", "edge"),
        ("k10temp", "Tctl"),
        ("acpitz", ""),
    ]
    for name, label in preferred:
        for item in items:
            if item["name"] == name and item["label"] == label:
                return item
    return max(items, key=lambda item: float(item["c"])) if items else None


def read_temperature() -> dict:
    items = read_hwmon_temperatures() + read_thermal_zones()
    primary = _match_temperature(items)
    return {
        "ok": primary is not None,
        "temperature_c": None if primary is None else primary["c"],
        "limit_c": TEMP_MAX_C,
        "sensor_name": None if primary is None else primary["name"],
        "sensor_label": None if primary is None else primary["label"],
        "sensor_path": None if primary is None else primary["path"],
        "sensors": items,
    }
