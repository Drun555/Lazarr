#!/bin/sh
set -eu

if [ "$(id -u)" = "0" ]; then
    puid="${PUID:-1000}"
    pgid="${PGID:-1000}"

    case "$puid:$pgid" in
        *[!0-9:]*|:*|*:) echo "PUID and PGID must be numeric" >&2; exit 64 ;;
    esac

    current_gid="$(id -g lazarr)"
    current_uid="$(id -u lazarr)"
    if [ "$current_gid" != "$pgid" ]; then
        groupmod --non-unique --gid "$pgid" lazarr
    fi
    if [ "$current_uid" != "$puid" ]; then
        usermod --non-unique --uid "$puid" --gid "$pgid" lazarr
    fi

    mkdir -p /data /plugins /downloads/movies /downloads/series
    chown -R "$puid:$pgid" /data /plugins
    chown "$puid:$pgid" /downloads /downloads/movies /downloads/series
    chmod 0700 /data
    exec gosu "$puid:$pgid" "$@"
fi

exec "$@"
