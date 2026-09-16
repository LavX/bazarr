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

# Update bazarr user/group IDs if they differ. -o allows the target UID/GID
# to be shared with an existing user/group, which is required on Unraid where
# PGID=100 (the host's "users" group) is already taken inside the container.
#
# Best effort only: both commands rewrite /etc, which a container started with
# read_only: true mounts read-only. The drop below uses the numbers rather than
# the name so it is correct either way. Going by name would be a trap here, as
# a rename that failed would silently leave everything running as UID 1000.
if [ "$(id -u bazarr)" != "$PUID" ]; then
    echo "Updating bazarr user UID to $PUID..."
    usermod -o -u "$PUID" bazarr 2>/dev/null || echo "Read-only /etc, running by UID instead."
fi

if [ "$(id -g bazarr)" != "$PGID" ]; then
    echo "Updating bazarr group GID to $PGID..."
    groupmod -o -g "$PGID" bazarr 2>/dev/null || echo "Read-only /etc, running by GID instead."
fi

# Fix ownership of key config paths synchronously (fast, only top-level)
#
# /config only. The application tree stays owned by root: a process running as
# PUID that can write /app/bazarr can replace the .py files the host imports on
# its next restart, which leaves the Provider Hub's worker subprocess a fault
# boundary and nothing more. Everything that writes at runtime writes under
# /config now, including the binaries downloader (utilities/binaries.py).
echo "Setting permissions on /config..."
if ! chown "$PUID:$PGID" /config 2>/dev/null; then
    echo "Could not take ownership of /config. Dropping CAP_CHOWN takes this away;"
    echo "/config then has to be owned by $PUID:$PGID on the host already."
fi
chown -R "$PUID:$PGID" /config/config 2>/dev/null || true
chown -R "$PUID:$PGID" /config/db 2>/dev/null || true
chown -R "$PUID:$PGID" /config/log 2>/dev/null || true

# Deep permission fix runs in background (large config dirs can take minutes)
(chown -R "$PUID:$PGID" /config 2>/dev/null &)

# Run as the requested user using gosu. HOME is set explicitly because gosu
# takes it from the passwd entry, which is the thing usermod may not have been
# allowed to update.
echo "Starting Bazarr..."
exec gosu "$PUID:$PGID" env HOME=/config "$@"