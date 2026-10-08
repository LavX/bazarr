// The server holds the real confirmation window: these helpers only remember
// that a confirmation just succeeded, so the prompt is not repeated for every
// gated action inside it. Any refusal clears the memory, so a stale entry (a
// restart, a clock change) never swallows a prompt for good.
const CONFIRM_WINDOW_MS = 10 * 60 * 1000;

let confirmedUntil = 0;

export function markPasswordConfirmed(): void {
  confirmedUntil = Date.now() + CONFIRM_WINDOW_MS;
}

export function passwordConfirmWindowLive(): boolean {
  return Date.now() < confirmedUntil;
}

export function forgetPasswordConfirmation(): void {
  confirmedUntil = 0;
}
