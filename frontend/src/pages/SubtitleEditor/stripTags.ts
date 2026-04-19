// Strip HTML-like tags and ASS override blocks from subtitle text.
// Loops until no more matches so nested or malformed tags like "<<script>"
// cannot survive a single pass. The result is only used for length counts
// and display, never written back into the DOM.
const TAG_RE = /<[^>]*>|\{\\[^}]*\}/g;

export function stripTags(text: string): string {
  let prev = "";
  let current = text;
  while (prev !== current) {
    prev = current;
    current = current.replace(TAG_RE, "");
  }
  return current;
}
