/**
 * Shared wording for the discovery feeds.
 *
 * Two things the feeds kept getting wrong on their own: counts that read as
 * "1 failed checks", and observation times printed to the second in a line the
 * reader is only scanning for freshness. The exact stamp is still there for
 * anyone who wants it, printed in full inside each freshness disclosure.
 */
export function plural(count: number, one: string, many = one + "s"): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** A time a reader can use: no seconds, and today reads as a time of day. */
export function readableTime(value: string): string {
  const when = new Date(value);
  const sameDay = when.toDateString() === new Date().toDateString();
  return sameDay
    ? when.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" })
    : when.toLocaleString(undefined, {
        dateStyle: "medium",
        timeStyle: "short",
      });
}
