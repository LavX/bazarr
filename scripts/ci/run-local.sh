#!/usr/bin/env bash
# Run the pull request CI pipeline on this machine, in parallel.
#
# Mirrors .github/workflows/ci.yml for a pull request into development: the
# frontend build, checks, unit tests and coverage floor; ruff and every backend
# pytest group, with the isolation files each in their own process; the
# application startup check; and the docs checks against origin/development.
# The suites are read out of ci.yml, so nothing here lists a test file.
#
# Usage: scripts/ci/run-local.sh [--backend-only | --frontend-only]
#                                [--python VERSION] [--jobs N]
#
#   --backend-only   ruff, pytest groups and the startup check (the UI is
#                    still built, because the backend jobs run with it)
#   --frontend-only  frontend build, checks, tests and coverage
#   --python X.Y     backend interpreter; default: the Docker image's version
#   --jobs N         processes at a time; default: the number of CPUs
#
# Needs python X.Y (found through uv when it is installed), node and npm, and
# docker for a throwaway PostgreSQL 16 on a random local port, removed on exit.
# The virtualenv is cached under ~/.cache/bazarr-ci and rebuilt when the
# requirements change. Exits non-zero when anything fails.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
MODE=all
PYTHON_VERSION=""
JOBS="$(nproc 2>/dev/null || echo 4)"

usage() { sed -n '2,23p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backend-only) MODE=backend ;;
    --frontend-only) MODE=frontend ;;
    --python) PYTHON_VERSION="${2:?--python needs a version, such as 3.13}"; shift ;;
    --python=*) PYTHON_VERSION="${1#*=}" ;;
    --jobs) JOBS="${2:?--jobs needs a number}"; shift ;;
    --jobs=*) JOBS="${1#*=}" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

STARTED=$(date +%s)
CACHE="${BAZARR_CI_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/bazarr-ci}"
mkdir -p "$CACHE"
WORK="$(mktemp -d -t bazarr-ci-local.XXXXXX)"
LOGS="$CACHE/logs/$(date +%Y%m%d-%H%M%S)-$$"
PG_NAME=""

cleanup() {
  if [[ -n "$PG_NAME" ]]; then
    docker rm -f "$PG_NAME" >/dev/null 2>&1 || true
  fi
  rm -rf "$WORK"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say() { printf '==> %s\n' "$*"; }

if [[ -z "$PYTHON_VERSION" ]]; then
  PYTHON_VERSION="$(sed -nE 's/^FROM[[:space:]]+python:([0-9]+\.[0-9]+).*/\1/p' "$ROOT/Dockerfile" | head -n1)"
fi

# The UI is built in every mode, because the backend jobs run with it.
command -v npm >/dev/null || { echo "npm is required (node from frontend/.nvmrc)" >&2; exit 1; }
LOCK_STAMP="$(cat "$ROOT/frontend/package-lock.json" <(node --version) | sha256sum | cut -c1-16)"
if [[ "$(cat "$ROOT/frontend/node_modules/.bazarr-ci-stamp" 2>/dev/null)" != "$LOCK_STAMP" ]]; then
  say "Installing frontend dependencies (npm ci)"
  (cd "$ROOT/frontend" && npm ci --no-audit --no-fund --loglevel=error)
  echo "$LOCK_STAMP" > "$ROOT/frontend/node_modules/.bazarr-ci-stamp"
fi

ARGS=(--mode "$MODE" --python "$PYTHON_VERSION" --jobs "$JOBS" --logs "$LOGS"
      --work "$WORK" --history "$CACHE/durations.json")

if [[ "$MODE" != frontend ]]; then
  PYTHON=""
  if command -v uv >/dev/null; then
    # The newest installed patch release, as setup-python picks in CI; a
    # release candidate earlier on PATH would otherwise win.
    PYTHON="$(uv python list "$PYTHON_VERSION" --only-installed --output-format json 2>/dev/null \
      | python3 -c '
import json, re, sys
entries = [e for e in json.load(sys.stdin)
           if e.get("implementation") == "cpython" and e.get("variant", "default") == "default"]
def key(e):
    parts = re.findall(r"\d+|[a-z]+", e["version"])
    final = not re.search(r"[a-z]", e["version"])
    return ([int(p) for p in parts[:3] if p.isdigit()], final)
if entries:
    print(max(entries, key=key)["path"])
' 2>/dev/null || true)"
  fi
  if [[ -z "$PYTHON" ]]; then
    PYTHON="$(command -v "python$PYTHON_VERSION" || true)"
  fi
  if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c "import sys; sys.exit(f'{sys.version_info[0]}.{sys.version_info[1]}' != '$PYTHON_VERSION')"; then
    echo "Python $PYTHON_VERSION was not found. Install it, or pass --python." >&2
    exit 1
  fi

  VENV="$CACHE/venv-$PYTHON_VERSION"
  VENV_STAMP="$(cat "$ROOT/requirements.txt" "$ROOT/dev-requirements.txt" <("$PYTHON" -VV) <(echo "$PYTHON") | sha256sum | cut -c1-16)"
  if [[ "$(cat "$VENV/.bazarr-ci-stamp" 2>/dev/null)" != "$VENV_STAMP" ]]; then
    say "Creating the Python $PYTHON_VERSION virtualenv in $VENV"
    rm -rf "$VENV"
    if command -v uv >/dev/null; then
      uv venv -q --seed --python "$PYTHON" "$VENV"
    else
      "$PYTHON" -m venv "$VENV"
    fi
    # The same installs as the CI backend jobs, psycopg included.
    "$VENV/bin/pip" install -q -r "$ROOT/requirements.txt"
    "$VENV/bin/pip" install -q --no-deps signalrcore==1.0.2
    "$VENV/bin/pip" install -q -r "$ROOT/dev-requirements.txt"
    "$VENV/bin/pip" install -q "psycopg[binary]"
    echo "$VENV_STAMP" > "$VENV/.bazarr-ci-stamp"
  fi
  ORCHESTRATOR_PYTHON="$VENV/bin/python"

  command -v docker >/dev/null || { echo "docker is required for the PostgreSQL suites" >&2; exit 1; }
  # A name of our own, so the containers this machine may already run
  # (bazarr, bazarr-standalone, bazarr-atlas) are never touched.
  PG_NAME="bazarr-ci-local-$$-$RANDOM"
  say "Starting a throwaway PostgreSQL 16 ($PG_NAME)"
  docker run -d --rm --name "$PG_NAME" \
    -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=bazarr_test \
    --tmpfs /var/lib/postgresql/data \
    -p 127.0.0.1::5432 postgres:16-alpine >/dev/null
  for _ in $(seq 1 60); do
    if docker exec "$PG_NAME" pg_isready -q -h 127.0.0.1 -U postgres 2>/dev/null; then
      break
    fi
    sleep 1
  done
  docker exec "$PG_NAME" pg_isready -q -h 127.0.0.1 -U postgres
  PG_PORT="$(docker port "$PG_NAME" 5432/tcp | head -n1 | sed 's/.*://')"
  CREATE=()
  for index in $(seq 1 "$JOBS"); do
    CREATE+=(-c "CREATE DATABASE bazarr_test_$index")
  done
  docker exec "$PG_NAME" psql -q -U postgres "${CREATE[@]}" >/dev/null
  ARGS+=(--venv "$VENV" --pg-port "$PG_PORT" --databases "$JOBS")
else
  ORCHESTRATOR_PYTHON="$(command -v python3)"
  "$ORCHESTRATOR_PYTHON" -c "import yaml" 2>/dev/null || {
    echo "--frontend-only reads ci.yml and needs PyYAML for python3" >&2; exit 1; }
fi

if git -C "$ROOT" rev-parse --verify -q origin/development >/dev/null; then
  ARGS+=(--docs-base origin/development)
else
  ARGS+=(--docs-base HEAD)
fi

say "Setup took $(( $(date +%s) - STARTED ))s"
status=0
"$ORCHESTRATOR_PYTHON" "$ROOT/scripts/ci/local_ci.py" "${ARGS[@]}" || status=$?
say "Total, setup included: $(( $(date +%s) - STARTED ))s. Logs: $LOGS"
exit "$status"
