import {
  readSessionValue,
  removeSessionValue,
  writeSessionValue,
} from "./browserStorage";

const RESTART_RELOAD_STORAGE_KEY = "bazarr.restart.reload_after_reconnect";

export function markRestartReloadPending() {
  writeSessionValue(RESTART_RELOAD_STORAGE_KEY, "1");
}

export function consumeRestartReloadPending() {
  const pending = readSessionValue(RESTART_RELOAD_STORAGE_KEY) === "1";
  if (pending) {
    removeSessionValue(RESTART_RELOAD_STORAGE_KEY);
  }
  return pending;
}
