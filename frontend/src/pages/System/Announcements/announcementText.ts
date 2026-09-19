/**
 * What a reader scans by, derived from a raw announcement.
 *
 * The feed's schema carries no type and no title. The API hands over one blob
 * of prose, a link, an already-rendered age and a dismissal flag, so every
 * scannable property has to come out of the prose. It is derived here, in pure
 * functions, rather than inline in the page, so the rules are stated once and
 * can be read without a browser.
 */

/**
 * The kind a reader sorts by. The app's badge theme paints every badge the
 * same neutral chip whatever `color` it is given, so the kind is carried by
 * its word in a fixed position rather than by a colour that would not render.
 */
export const ANNOUNCEMENT_LABELS = {
  notice: "Notice",
  provider: "Provider Hub",
  release: "Release",
} as const;

export type AnnouncementKind = keyof typeof ANNOUNCEMENT_LABELS;

/**
 * A release names the version it ships, and that token is the stronger signal:
 * the v2.3.0 note goes on to introduce the Provider Hub, so testing the phrase
 * first would file a release under the provider section. A provider entry
 * names no version, so the order costs nothing in the other direction.
 */
const VERSION = /\bv\d+\.\d+\.\d+\b/i;
const PROVIDER_HUB = /provider hub/i;

/** A guess about a word in the text, and only ever drives a badge. */
export function classifyAnnouncement(text: string): AnnouncementKind {
  if (VERSION.test(text)) {
    return "release";
  }
  if (PROVIDER_HUB.test(text)) {
    return "provider";
  }
  return "notice";
}

/**
 * Longer than this and the lead is not a headline, it is the body. Clamping a
 * three-hundred-character sentence into a one-line title would hide the rest
 * of the text behind a heading that says no more than the wall it replaced.
 */
const MAX_HEADLINE = 160;

export interface AnnouncementParts {
  body: string;
  headline?: string;
}

/**
 * Every announcement in the feed opens with a self-contained lead sentence
 * ("7 new subtitle providers have joined the Provider Hub!"), which is exactly
 * the line a reader needs to decide whether to read on. Split there so the
 * card can carry a title, and leave a one-sentence announcement as its own
 * headline rather than orphaning it into the body.
 */
export function splitAnnouncementText(text: string): AnnouncementParts {
  const trimmed = text.trim();
  if (!trimmed) {
    return { body: "" };
  }

  const lead = /^([\s\S]*?[.!?])(?:\s|$)/.exec(trimmed);
  const headline = lead ? lead[1] : trimmed;
  if (headline.length > MAX_HEADLINE) {
    return { body: trimmed };
  }

  const body = (lead ? trimmed.slice(lead[0].length) : "").trim();
  return { body, headline };
}

/**
 * Where a link goes, in the reader's words. The feed labels nothing, so the
 * host is the only part of the destination that is always true; the word
 * "Link" told the reader nothing and the full URL is too long for a card.
 * Returns undefined for a value that is not an absolute URL, and the caller
 * falls back to a generic label rather than guessing at the destination.
 */
export function linkHost(link: string): string | undefined {
  try {
    return new URL(link).hostname.replace(/^www\./, "") || undefined;
  } catch {
    return undefined;
  }
}

export interface TextSegment {
  link?: string;
  text: string;
}

/**
 * Splits a body into prose and the URLs inside it, so a URL the feed dropped
 * mid-sentence is still reachable. Scheme-bearing URLs only: a bare
 * "github.com/LavX/..." left as prose is a small loss, while guessing where a
 * hostname begins would turn ordinary sentences into links. A trailing
 * sentence mark stays in the prose, and a closing bracket around a URL is
 * punctuation rather than part of the address.
 */
export function splitLinks(text: string): TextSegment[] {
  const pattern = /https?:\/\/[^\s<>()]*[^\s<>().,;:!?]/g;
  const segments: TextSegment[] = [];
  let cursor = 0;
  let match = pattern.exec(text);

  while (match) {
    if (match.index > cursor) {
      segments.push({ text: text.slice(cursor, match.index) });
    }
    segments.push({ link: match[0], text: match[0] });
    cursor = match.index + match[0].length;
    match = pattern.exec(text);
  }

  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor) });
  }

  return segments;
}
