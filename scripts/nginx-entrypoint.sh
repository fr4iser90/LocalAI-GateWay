#!/bin/sh
# Render nginx.conf (path-based API gateway) and start nginx.
set -eu

# Shown in server_name; default_server still accepts any Host (local compose).
export PUBLIC_HOST="${PUBLIC_HOST:-_}"

envsubst '$PUBLIC_HOST' \
  < /etc/nginx/templates/nginx.conf.template > /etc/nginx/nginx.conf

exec nginx -g 'daemon off;'
