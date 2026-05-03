#!/usr/bin/env bash
# Profile bazarr's allocator with memray. Install: pip install memray
#
# Usage:
#   ./profile-with-memray.sh run                  # start under memray, generate flame
#   ./profile-with-memray.sh run --no-live        # same, but record-only (no live TUI)
#   ./profile-with-memray.sh attach <pid>         # attach to a running bazarr
#
# Notes:
#  - Memray adds 2-3x runtime overhead. Use only on a dev/test instance.
#  - Output goes to /tmp/bazarr-memray-<timestamp>.bin
#  - Requires Python 3.7+; tested with memray 1.x.

set -euo pipefail

usage() {
    cat <<'USAGE'
profile-with-memray.sh: wrap memray for a bazarr profiling session.

Usage:
  ./profile-with-memray.sh run                  start bazarr under memray, --live mode
  ./profile-with-memray.sh run --no-live        same, but record-only (background-friendly)
  ./profile-with-memray.sh attach <pid>         attach memray to a running bazarr pid

Output:
  Recordings: /tmp/bazarr-memray-<timestamp>.bin
  Flame graph (record mode): /tmp/bazarr-memray-<timestamp>.html
USAGE
}

if ! command -v memray >/dev/null 2>&1; then
    echo "memray is not installed. Install with: pip install memray" >&2
    exit 1
fi

if [ "$#" -lt 1 ]; then
    usage
    exit 1
fi

MODE="$1"
shift || true

TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
OUTPUT="/tmp/bazarr-memray-${TIMESTAMP}.bin"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BAZARR_ENTRY="${REPO_ROOT}/bazarr.py"

case "${MODE}" in
    run)
        if [ ! -f "${BAZARR_ENTRY}" ]; then
            echo "Cannot find bazarr.py at ${BAZARR_ENTRY}" >&2
            exit 1
        fi
        LIVE_FLAG="--live"
        if [ "${1:-}" = "--no-live" ]; then
            LIVE_FLAG=""
            shift || true
        fi
        echo "Recording allocations to ${OUTPUT}"
        if [ -n "${LIVE_FLAG}" ]; then
            echo "Live TUI enabled. Press q to quit."
            memray run "${LIVE_FLAG}" -o "${OUTPUT}" "${BAZARR_ENTRY}" "$@"
        else
            memray run -o "${OUTPUT}" "${BAZARR_ENTRY}" "$@"
            echo "Generating flame graph from ${OUTPUT}"
            memray flamegraph "${OUTPUT}"
            echo "Done. HTML report next to ${OUTPUT}"
        fi
        ;;
    attach)
        TARGET_PID="${1:-}"
        if [ -z "${TARGET_PID}" ]; then
            echo "attach requires a pid argument" >&2
            usage
            exit 1
        fi
        echo "Attaching memray to pid ${TARGET_PID}, recording to ${OUTPUT}"
        memray attach "${TARGET_PID}" -o "${OUTPUT}"
        echo "Detached. Recording at ${OUTPUT}"
        ;;
    -h|--help|help)
        usage
        ;;
    *)
        echo "Unknown mode: ${MODE}" >&2
        usage
        exit 1
        ;;
esac
