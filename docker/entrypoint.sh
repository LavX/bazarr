#!/bin/bash
set -e

# Set timezone if TZ is specified
if [ -n "$TZ" ]; then
    echo "Setting timezone to $TZ"
    cp /usr/share/zoneinfo/$TZ /etc/localtime 2>/dev/null || true
    echo "$TZ" > /etc/timezone 2>/dev/null || true
fi

# Handle PUID/PGID for permissions
PUID=${PUID:-1000}
PGID=${PGID:-1000}

# Update user/group IDs if different from default
if [ "$PUID" != "1000" ] || [ "$PGID" != "1000" ]; then
    echo "Setting user to $PUID:$PGID"
    deluser bazarr 2>/dev/null || true
    delgroup bazarr 2>/dev/null || true
    addgroup -g "$PGID" bazarr
    adduser -u "$PUID" -G bazarr -h /config -D bazarr
fi

# Ensure config directory is owned by bazarr user
chown -R bazarr:bazarr /config 2>/dev/null || true

# Run as bazarr user
exec su-exec bazarr:bazarr python3 /app/bazarr/bin/bazarr.py --no-update --config /config "$@"