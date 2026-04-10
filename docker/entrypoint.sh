#!/bin/bash
# =============================================================================
# Bazarr Docker Entrypoint
# =============================================================================
# Handles:
# - User/Group ID mapping (PUID/PGID)
# - Permissions setup
# - Application startup
# =============================================================================

set -e

# Default values
PUID=${PUID:-1000}
PGID=${PGID:-1000}

echo '
__/\\\\\\\\\\\\\_________________________________________________________________________________________
 _\/\\\/////////\\\_______________________________________________________________________________________
  _\/\\\_______\/\\\______________________________________________________________________________/\\\_____
   _\/\\\\\\\\\\\\\\___/\\\\\\\\\_____/\\\\\\\\\\\__/\\\\\\\\\_____/\\/\\\\\\\___/\\/\\\\\\\______\/\\\_____
    _\/\\\/////////\\\_\////////\\\___\///////\\\/__\////////\\\___\/\\\/////\\\_\/\\\/////\\\__/\\\\\\\\\\\_
     _\/\\\_______\/\\\___/\\\\\\\\\\_______/\\\/______/\\\\\\\\\\__\/\\\___\///__\/\\\___\///__\/////\\\///__
      _\/\\\_______\/\\\__/\\\/////\\\_____/\\\/_______/\\\/////\\\__\/\\\_________\/\\\_____________\/\\\_____
       _\/\\\\\\\\\\\\\/__\//\\\\\\\\/\\__/\\\\\\\\\\\_\//\\\\\\\\/\\_\/\\\_________\/\\\_____________\///_____
        _\/////////////_____\////////\//__\///////////___\////////\//__\///__________\///________________________

Repository: https://github.com/LavX/bazarr
'

echo "Starting with UID: $PUID, GID: $PGID"

# Update bazarr user/group IDs if they differ
if [ "$(id -u bazarr)" != "$PUID" ]; then
    echo "Updating bazarr user UID to $PUID..."
    usermod -u $PUID bazarr
fi

if [ "$(id -g bazarr)" != "$PGID" ]; then
    echo "Updating bazarr group GID to $PGID..."
    groupmod -g $PGID bazarr
fi

# Fix ownership of key config paths synchronously (fast, only top-level)
echo "Setting permissions on /config..."
chown bazarr:bazarr /config
chown -R bazarr:bazarr /config/config 2>/dev/null || true
chown -R bazarr:bazarr /config/db 2>/dev/null || true
chown -R bazarr:bazarr /config/log 2>/dev/null || true
chown bazarr:bazarr /app/bazarr

# Deep permission fix runs in background (large config dirs can take minutes)
(chown -R bazarr:bazarr /config /app/bazarr 2>/dev/null &)

# Run as bazarr user using gosu
echo "Starting Bazarr..."
exec gosu bazarr "$@"