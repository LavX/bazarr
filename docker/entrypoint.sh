#!/bin/bash
# =============================================================================
# Bazarr Docker Entrypoint
# =============================================================================
# Handles:
# - User/Group ID mapping (PUID/PGID), skipped when started non-root
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

# Started non-root (Compose `user:`, docker run --user): there is nothing to
# drop. Changing groups would fail because setgroups needs root, even when the
# ids already match. Changing the bazarr user or group and
# taking ownership of /config are root-only too, and the PUID and PGID
# variables have no meaning here, so skip all of it and run the app as this
# user. /config has to be writable by it already. Same tini flags and HOME as
# the root path at the end of this file, minus the privilege drop.
if [ "$(id -u)" != "0" ]; then
    uidgid="$(id -u):$(id -g)"
    echo "Started as non-root $uidgid; PUID/PGID are ignored and /config must already be writable by this user."
    if ! [ -d /config ]; then
        echo "ERROR: /config is missing or is not a directory. Mount a directory at /config." >&2
        exit 1
    fi
    # A real write, not [ -w ]: access(2) says yes for a read-only bind mount.
    probe="/config/.write-probe.$$"
    if ( : > "$probe" ) 2>/dev/null; then
        rm -f "$probe"
    elif [ -w /config ]; then
        echo "ERROR: /config accepts no new files for $uidgid although its permissions allow it." >&2
        echo "It is probably mounted read-only (remove :ro), or the disk is full." >&2
        exit 1
    else
        echo "ERROR: /config is not writable by $uidgid. Give this user ownership of the config" >&2
        echo "directory on the host (chown -R), remove :ro if it is mounted read-only, or start" >&2
        echo "the container as root and use PUID/PGID." >&2
        exit 1
    fi
    # Existing subdirectories left over from an older PUID would fail later.
    for sub in config db log; do
        if [ -d "/config/$sub" ] && ! [ -w "/config/$sub" ]; then
            echo "ERROR: /config/$sub is not writable by $uidgid. Run chown -R on the whole" >&2
            echo "config directory on the host, or start the container as root and use PUID/PGID." >&2
            exit 1
        fi
    done
    echo "Starting Bazarr..."
    exec /usr/bin/tini -s -- env HOME=/config "$@"
fi

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

# Preserve runtime-supplied supplementary groups, except root's primary group
# and group 0. Read the kernel group list, not id -G, which includes the primary
# group. Do not infer access from files or sockets in the container.
startup_gid=$(id -g)
supplementary_groups=
while read -r field groups; do
    if [ "$field" = 'Groups:' ]; then
        for group in $groups; do
            if [ "$group" != 0 ] && [ "$group" != "$startup_gid" ]; then
                supplementary_groups="${supplementary_groups:+$supplementary_groups,}$group"
            fi
        done
        break
    fi
done < /proc/self/status
group_option=(--clear-groups)
if [ -n "$supplementary_groups" ]; then
    group_option=(--groups "$supplementary_groups")
fi
# Use numeric IDs even if the passwd and group files could not be updated.
# Set HOME explicitly for the application.
#
# tini becomes PID 1 here (every step is an exec, so the PID never changes) and
# runs as the requested user, after the drop. It reaps orphaned processes and
# forwards SIGTERM and SIGINT to the command. It has to be the unprivileged
# side of the drop: a root tini cannot signal a PUID process once the container
# has dropped CAP_KILL, and the hardened compose files do drop it. -s keeps the
# reaping working when a runtime already put an init above this script, as
# docker run --init does.
echo "Starting Bazarr..."
exec setpriv --reuid "$PUID" --regid "$PGID" "${group_option[@]}" /usr/bin/tini -s -- env HOME=/config "$@"
