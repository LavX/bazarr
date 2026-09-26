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

/**
 * A calendar day from a feed record ("2026-09-09") in the reader's own date
 * words ("Sep 9, 2026"). Parsed as a local day so timezones cannot move it,
 * and returned untouched when it is not a plain day: inventing a nicer
 * reading of an unknown value would be worse than the raw one.
 */
export function readableFeedDate(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value.trim());
  if (!match) return value;
  const day = new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
  );
  if (
    Number.isNaN(day.getTime()) ||
    day.getFullYear() !== Number(match[1]) ||
    day.getMonth() !== Number(match[2]) - 1 ||
    day.getDate() !== Number(match[3])
  )
    return value;
  return day.toLocaleDateString(undefined, { dateStyle: "medium" });
}
