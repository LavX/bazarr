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

echo "
╔═══════════════════════════════════════════════════════════════╗
║            Bazarr (LavX Fork) Docker Container                ║
║                                                               ║
║  OpenSubtitles.org scraper support included                   ║
║  https://github.com/LavX/bazarr                               ║
╚═══════════════════════════════════════════════════════════════╝
"

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

# Fix ownership of config directory
echo "Setting permissions on /config..."
chown -R bazarr:bazarr /config

# Fix ownership of application directory
chown -R bazarr:bazarr /app/bazarr

# Run as bazarr user using gosu
echo "Starting Bazarr..."
exec gosu bazarr "$@"