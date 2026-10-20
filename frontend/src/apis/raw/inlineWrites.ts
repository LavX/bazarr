// A request whose failure the caller reports next to the control that made it
// skips the API client's generic "Error <code>" toast: the reader already has
// the inline alert, and a second alert-role toast in the far corner read the
// same failure out twice. It is opted into per call, by the hook a caller asks
// for inline errors, and never for a whole screen: a write with no handler of
// its own still toasts, wherever it is made.
//
// The flag is read by a synchronous request interceptor, so it covers the
// request a call issues before its first await. Every raw API method builds
// and sends its request before it awaits anything, so a wrapped method is
// covered, and anything it sends after an await (a job poll) is not.
let depth = 0;

/** Runs `call` so that the request it sends skips the client's toast. */
export function withInlineErrors<T>(call: () => Promise<T>): Promise<T> {
  depth += 1;
  try {
    return call();
  } finally {
    depth -= 1;
  }
}

/**
 * A mutation function that, when `inline` is set, runs through
 * withInlineErrors. It keeps the exact type it was given, so the mutation
 * infers the same variables and result as it did before it was wrapped.
 */
export function inlineWhen<F extends (...args: never[]) => Promise<unknown>>(
  inline: boolean,
  fn: F,
): F {
  if (!inline) {
    return fn;
  }
  return ((...args: Parameters<F>) =>
    withInlineErrors(() => fn(...args))) as unknown as F;
}

/** Whether the request being built now was asked to skip the toast. */
export function inlineErrorsRequested(): boolean {
  return depth > 0;
}
