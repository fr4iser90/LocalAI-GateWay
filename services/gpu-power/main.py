#!/usr/bin/env python3
"""GPU power probe for LocalAI-GateWay usage-pool coupling.

Reads instantaneous GPU power from sysfs hwmon (NVIDIA/AMD), optional nvidia-smi /
rocm-smi fallback. Intended to run on the GPU host (compose overlay or bare).

GET /power  → {"ok": true, "watts": 123.4, "source": "sysfs", "device": "..."}
GET /health → 204
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SYS_ROOT = Path(os.environ.get("SYS_ROOT", "/sys"))
PORT = int(os.environ.get("PORT", "8080"))


def _read_uwatts(path: Path) -> float | None:
    try:
        raw = path.read_text().strip()
        # microwatts (hwmon) or milliwatts on some boards
        val = float(raw)
        if val <= 0:
            return None
        # Heuristic: values > 1e6 are µW; 1e3–1e6 often mW; else already W
        if val > 1_000_000:
            return val / 1_000_000.0
        if val > 10_000:
            return val / 1_000.0
        return val
    except Exception:
        return None


def _scan_sysfs() -> tuple[float | None, str]:
    """Return (watts, device_label) from drm/hwmon power sensors."""
    best: tuple[float, str] | None = None
    drm = SYS_ROOT / "class" / "drm"
    if drm.is_dir():
        for card in sorted(drm.glob("card[0-9]")):
            device = card / "device"
            hwmon_root = device / "hwmon"
            if not hwmon_root.is_dir():
                continue
            for hw in hwmon_root.iterdir():
                for name in ("power1_average", "power1_input", "power2_average"):
                    p = hw / name
                    if not p.is_file():
                        continue
                    w = _read_uwatts(p)
                    if w is None:
                        continue
                    label = f"{card.name}/{name}"
                    if best is None or w > best[0]:
                        best = (w, label)
    # Fallback: any hwmon with *power*
    if best is None:
        hwmon = SYS_ROOT / "class" / "hwmon"
        if hwmon.is_dir():
            for hw in hwmon.iterdir():
                for p in hw.glob("power*_average"):
                    w = _read_uwatts(p)
                    if w is None:
                        continue
                    label = f"{hw.name}/{p.name}"
                    if best is None or w > best[0]:
                        best = (w, label)
                for p in hw.glob("power*_input"):
                    w = _read_uwatts(p)
                    if w is None:
                        continue
                    label = f"{hw.name}/{p.name}"
                    if best is None or w > best[0]:
                        best = (w, label)
    if best:
        return best[0], best[1]
    return None, ""


def _nvidia_smi() -> float | None:
    try:
        out = subprocess.check_output(
            [
                "nvidia-smi",
                "--query-gpu=power.draw",
                "--format=csv,noheader,nounits",
            ],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        vals = []
        for line in out.strip().splitlines():
            line = line.strip()
            if not line or line.upper() == "[N/A]":
                continue
            vals.append(float(line))
        return max(vals) if vals else None
    except Exception:
        return None


def _rocm_smi() -> float | None:
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showpower"],
            stderr=subprocess.DEVNULL,
            timeout=2,
            text=True,
        )
        # Average Graphics Package Power: 123.0 W
        m = re.findall(r"([0-9]+(?:\.[0-9]+)?)\s*W", out)
        if not m:
            return None
        return max(float(x) for x in m)
    except Exception:
        return None


def read_power() -> dict:
    watts, device = _scan_sysfs()
    if watts is not None:
        return {"ok": True, "watts": round(watts, 2), "source": "sysfs", "device": device}
    watts = _nvidia_smi()
    if watts is not None:
        return {"ok": True, "watts": round(watts, 2), "source": "nvidia-smi", "device": "gpu"}
    watts = _rocm_smi()
    if watts is not None:
        return {"ok": True, "watts": round(watts, 2), "source": "rocm-smi", "device": "gpu"}
    return {"ok": False, "watts": None, "source": "none", "device": ""}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path in ("/health", "/check"):
            self.send_response(204)
            self.end_headers()
            return
        if path == "/power":
            body = json.dumps(read_power()).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"gpu-power listening on :{PORT} SYS_ROOT={SYS_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
