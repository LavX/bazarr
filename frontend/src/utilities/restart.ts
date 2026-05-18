const RESTART_RELOAD_STORAGE_KEY = "bazarr.restart.reload_after_reconnect";

export function markRestartReloadPending() {
  sessionStorage.setItem(RESTART_RELOAD_STORAGE_KEY, "1");
}

export function consumeRestartReloadPending() {
  const pending = sessionStorage.getItem(RESTART_RELOAD_STORAGE_KEY) === "1";
  if (pending) {
    sessionStorage.removeItem(RESTART_RELOAD_STORAGE_KEY);
  }
  return pending;
}
