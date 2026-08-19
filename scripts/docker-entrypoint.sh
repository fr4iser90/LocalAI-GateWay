#!/bin/sh
# Chown /data to host PUID/PGID (auto from compose: ${UID}/${GID}), then drop privileges.
set -e
data="${DATA_DIR:-/data}"
run_uid="${PUID:-10001}"
run_gid="${PGID:-10001}"
mkdir -p "$data"
if [ "$(id -u)" = "0" ]; then
  chown -R "${run_uid}:${run_gid}" "$data"
  exec su-exec "${run_uid}:${run_gid}" "$@"
fi
exec "$@"
