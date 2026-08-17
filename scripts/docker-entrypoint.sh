#!/bin/sh
# Drop privileges: fix /data ownership on volume mount, then run as non-root.
set -e
data="${DATA_DIR:-/data}"
mkdir -p "$data"
chown -R app:app "$data"
exec su-exec app:app "$@"
