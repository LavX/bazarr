// How many mounted surfaces report every failed write next to the control
// that made it (the setup wizard). While one is, a failed write skips the API
// client's generic toast: the reader already has the inline alert, and a
// second alert-role toast in the far corner read the same failure out twice.
// Reads still toast, because a failed read on those screens says nothing of
// its own. Kept apart from the client so a hook can claim the scope without
// loading the client and its socket.
let scopes = 0;

/** Claims failed writes for a surface; call the result to release them. */
export function ownWriteErrors(): () => void {
  scopes += 1;
  let released = false;
  return () => {
    if (!released) {
      released = true;
      scopes -= 1;
    }
  };
}

/** Whether a failed request with this method is a claimed write. */
export function ownsWriteError(method: string | undefined): boolean {
  return scopes > 0 && (method ?? "get").toLowerCase() !== "get";
}
