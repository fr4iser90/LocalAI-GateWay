#!/usr/bin/env bash
# Full-page screenshot of the GIC landing for VL integration tests.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$ROOT/tests/fixtures"
URL="${1:-https://www.generalintelligencecompany.com/?ref=a1.gallery}"
PNG="$OUT_DIR/gic_landing.png"
JPG="$OUT_DIR/gic_landing.jpg"

mkdir -p "$OUT_DIR"
PROFILE="$(mktemp -d /tmp/ff-shot-XXXXXX)"
cleanup() { rm -rf "$PROFILE"; }
trap cleanup EXIT

echo "Capturing $URL → $PNG"
firefox --headless --profile "$PROFILE" --window-size=1440,3000 \
  --screenshot="$PNG" "$URL"
if command -v magick >/dev/null 2>&1; then
  magick "$PNG" -resize 1024x -quality 85 "$JPG"
elif command -v convert >/dev/null 2>&1; then
  convert "$PNG" -resize 1024x -quality 85 "$JPG"
else
  echo "ImageMagick not found; keeping PNG only (may be large for VL)."
  exit 0
fi
rm -f "$PNG"
ls -la "$JPG"
echo "Done. VL test uses: $JPG (or INTEGRATION_VL_IMAGE=…)"
