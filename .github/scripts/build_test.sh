#!/bin/bash

# BAZARR_SMOKE_PORT and BAZARR_SMOKE_CONFIG let scripts/ci/run-local.sh start
# the application on a free port with a throwaway configuration directory, so
# a workstation that already runs Bazarr+ on 6767 is neither hit nor reused.
# CI sets neither and gets the defaults.
PORT="${BAZARR_SMOKE_PORT:-6767}"
ARGS=(--no-update)
if [[ -n "${BAZARR_SMOKE_CONFIG}" ]]; then
  ARGS+=(--config "${BAZARR_SMOKE_CONFIG}" --port "${PORT}")
fi

python3 "${ROOT_DIRECTORY}"/bazarr.py "${ARGS[@]}" &
PID=$!

sleep 30

if kill -s 0 $PID
then
  echo "Bazarr+ is still running. We'll test if UI is working..."
else
  exit 1
fi

exitcode=0
curl -fsSL --retry-all-errors --retry 60 --retry-max-time 120 --max-time 10 "http://127.0.0.1:${PORT}" --output /dev/null || exitcode=$?
[[ ${exitcode} == 0 ]] && echo "UI is responsive, good news!" || echo "Oops, UI isn't reachable, bad news..."

echo "Let's stop Bazarr before we exit..."
pkill -INT -P $PID

exit ${exitcode}
