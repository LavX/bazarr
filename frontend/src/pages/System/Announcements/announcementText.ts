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
 * A link that ends in punctuation the address does not own. A closing bracket
 * that wraps a URL in prose is punctuation, but one the address itself opened
 * belongs to it, so the two are counted rather than assumed.
 */
function trimUrl(raw: string): string {
  let url = raw.replace(/[.,;:!?'"]+$/, "");
  const closers = () => (url.match(/\)/g) ?? []).length;
  const openers = () => (url.match(/\(/g) ?? []).length;
  while (url.endsWith(")") && closers() > openers()) {
    url = url.slice(0, -1);
  }
  return url;
}

/**
 * Splits a body into prose and the URLs inside it, so a URL the feed dropped
 * mid-sentence is still reachable. The match runs to the next whitespace and
 * the address is then trimmed of punctuation the prose owns, which keeps a
 * bracketed URL intact instead of cutting it at the first bracket it contains.
 * A scheme is required: a bare "github.com/LavX/..." left as prose is a small
 * loss, while guessing where a hostname begins turns ordinary sentences into
 * links.
 */
export function splitLinks(text: string): TextSegment[] {
  const pattern = /https?:\/\/[^\s<>]+/g;
  const segments: TextSegment[] = [];
  let cursor = 0;
  let match = pattern.exec(text);

  while (match) {
    const url = trimUrl(match[0]);
    if (match.index > cursor) {
      segments.push({ text: text.slice(cursor, match.index) });
    }
    segments.push({ link: url, text: url });
    cursor = match.index + url.length;
    match = pattern.exec(text);
  }

  if (cursor < text.length) {
    segments.push({ text: text.slice(cursor) });
  }

  return segments;
}

/**
 * A name for a card that has no headline, where the lead was too long to read
 * as one. An article with no name leaves a screen reader with an unnamed
 * landmark and a list of dismiss buttons that all read the same, so the
 * opening of the body stands in for the title it does not have.
 */
export function bodyLabel(body: string, limit = 80): string {
  const flat = body.replace(/\s+/g, " ").trim();
  if (flat.length <= limit) {
    return flat;
  }
  const cut = flat.slice(0, limit);
  const lastSpace = cut.lastIndexOf(" ");
  return lastSpace > 0 ? cut.slice(0, lastSpace) : cut;
}
