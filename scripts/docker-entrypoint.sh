#!/bin/sh
# If running as root: fix /data ownership, then drop to app user.
# If already running as non-root (via compose user:): skip privilege drop.
set -e
data="${DATA_DIR:-/data}"
mkdir -p "$data"
if [ "$(id -u)" = "0" ]; then
  chown -R app:app "$data"
  exec su-exec app:app "$@"
else
  exec "$@"
fi
