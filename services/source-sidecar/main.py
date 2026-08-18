#!/usr/bin/env python3
"""Source host sidecar for GPU power and thermal guard data."""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
if str(THIS_DIR) not in sys.path:
    sys.path.insert(0, str(THIS_DIR))

import power
import thermal

PORT = int(os.environ.get("PORT", "8080"))

# Keep these aliases so tests and future callers do not need to know the split.
read_power = power.read_power
read_temperature = thermal.read_temperature
TEMP_MAX_C = thermal.TEMP_MAX_C
_scan_sysfs_power = power._scan_sysfs_power


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return

    def _send_json(self, status: int, payload: dict) -> None:
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_temp_check(self) -> None:
        payload = read_temperature()
        if not payload["ok"]:
            self._send_json(500, {"status": "error", **payload})
            return
        temp_c = float(payload["temperature_c"])
        if temp_c > TEMP_MAX_C:
            self._send_json(403, {"status": "blocked", **payload})
            return
        self.send_response(204)
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self.send_response(204)
            self.end_headers()
            return
        if path == "/power":
            self._send_json(200, read_power())
            return
        if path == "/thermal":
            self._send_json(200, read_temperature())
            return
        if path in ("/check", "/thermal/check"):
            self._handle_temp_check()
            return
        self.send_response(404)
        self.end_headers()


def main() -> None:
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"source-sidecar listening on :{PORT} SYS_ROOT={power.SYS_ROOT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
