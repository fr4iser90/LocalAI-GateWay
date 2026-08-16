import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


THERMAL_ROOT = Path(os.getenv("TEMP_THERMAL_ROOT", "/host/sys/class/thermal"))
SENSOR_TYPE = os.getenv("TEMP_SENSOR_TYPE", "x86_pkg_temp")
SENSOR_PATH = os.getenv("TEMP_SENSOR_PATH")
MAX_C = float(os.getenv("TEMP_MAX_C", "30"))


def find_sensor_path() -> Path:
    if SENSOR_PATH:
        return Path(SENSOR_PATH)

    for zone in sorted(THERMAL_ROOT.glob("thermal_zone*")):
        type_path = zone / "type"
        temp_path = zone / "temp"
        if not type_path.exists() or not temp_path.exists():
            continue
        if type_path.read_text(encoding="utf-8").strip() == SENSOR_TYPE:
            return temp_path

    raise FileNotFoundError(f"thermal sensor type not found: {SENSOR_TYPE}")


def read_temperature_c() -> float:
    raw = find_sensor_path().read_text(encoding="utf-8").strip()
    return int(raw) / 1000


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/check":
            self.send_response(404)
            self.end_headers()
            return

        try:
            temp_c = read_temperature_c()
        except Exception as exc:
            print(f"temperature check failed: {exc}", file=sys.stderr, flush=True)
            self.respond(500, {"status": "error", "error": str(exc)})
            return

        if temp_c > MAX_C:
            print(
                f"temperature blocked: sensor={SENSOR_TYPE} temp_c={temp_c:.1f} limit_c={MAX_C:.1f}",
                file=sys.stderr,
                flush=True,
            )
            self.respond(
                403,
                {
                    "status": "blocked",
                    "temperature_c": temp_c,
                    "limit_c": MAX_C,
                    "sensor_type": SENSOR_TYPE,
                },
            )
            return

        print(
            f"temperature allowed: sensor={SENSOR_TYPE} temp_c={temp_c:.1f} limit_c={MAX_C:.1f}",
            file=sys.stderr,
            flush=True,
        )
        self.send_response(204)
        self.end_headers()

    def respond(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        return


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
