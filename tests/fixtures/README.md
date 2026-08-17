# Integration fixtures

## GIC landing screenshot (VL bake-off)

Reference for the **landing reproduction** test:
text model gets a written brief; VL model gets this screenshot and both must output HTML.

### Capture / refresh

```bash
./scripts/capture_gic_fixture.sh
# or drop your own full-page PNG and:
magick your.png -resize 1280x -quality 88 tests/fixtures/gic_landing.jpg
export INTEGRATION_VL_IMAGE=/path/to/screenshot.jpg
```

### After the test

Open in the run folder:

- `compare_landings.html` — links + watts/tokens
- `chat_*_landing.html` — **what each model produced** (the actual pages)
- `chat_*_metrics.html` / `*_power.json` — cost only
