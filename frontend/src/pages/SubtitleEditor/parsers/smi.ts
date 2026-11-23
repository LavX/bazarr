import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";
import { cueId } from "./uuid";

function stripHtml(html: string): string {
  // Normalise <br> to newlines, then let the browser's HTML parser handle
  // tag removal and entity decoding. DOMParser is the CodeQL-recognised
  // robust sanitizer for js/incomplete-multi-character-sanitization: no
  // regex can survive nested/malformed tag bypasses, but a real HTML
  // parser cannot be tricked. The result is only used for display text
  // and character counts, never reinjected as HTML.
  const withBreaks = html.replace(/<br\s*\/?>/gi, "\n");
  const doc = new DOMParser().parseFromString(withBreaks, "text/html");
  return (doc.body.textContent ?? "").replace(/\u00a0/g, " ").trim();
}

export const smiParser: SubtitleParser = {
  parse(content, { strict } = {}): ParseResult {
    const normalized = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!normalized.trim()) {
      return { metadata: { format: "smi" }, cues: [] };
    }

    const text = strict
      ? normalized.replace(/<!--[\s\S]*?-->/g, "")
      : normalized;

    // Find all SYNC tags with their start times and collect text between them
    const syncPattern = /<SYNC\b(?:\s+Start\s*=\s*(\d+)\s*>)?/gi;
    const syncs: { startMs: number; index: number }[] = [];
    let match: RegExpExecArray | null;

    while ((match = syncPattern.exec(text)) !== null) {
      if (match[1] === undefined) {
        if (strict) throw new Error("Invalid SAMI cue timing");
        continue;
      }
      syncs.push({
        startMs: parseInt(match[1], 10),
        index: match.index + match[0].length,
      });
    }

    // Extract title if present
    let title: string | undefined;
    const titleMatch = text.match(/<TITLE[^>]*>([\s\S]*?)<\/TITLE>/i);
    if (titleMatch) {
      title = stripHtml(titleMatch[1]);
    }

    const cues = [];

    for (let i = 0; i < syncs.length; i++) {
      const start = syncs[i];
      // Get text from after this SYNC tag to the start of the next SYNC tag (or end of text)
      const nextSyncPattern = /<SYNC\s/i;
      const remainingText = text.slice(start.index);
      const nextSyncMatch = remainingText.match(nextSyncPattern);
      const segment = nextSyncMatch
        ? remainingText.slice(0, nextSyncMatch.index)
        : remainingText;
      const displayText = stripHtml(segment);

      // Skip empty/whitespace-only cues (often used as "clear" markers)
      if (!displayText || displayText === "&nbsp;" || !displayText.trim()) {
        continue;
      }

      const endMs =
        i + 1 < syncs.length ? syncs[i + 1].startMs : start.startMs + 3000;

      cues.push({
        id: cueId(),
        startMs: start.startMs,
        endMs,
        text: displayText,
      });
    }

    if (
      strict &&
      !cues.length &&
      !/^<SAMI\b[^>]*>[\s\S]*<\/SAMI>$/i.test(text.trim())
    ) {
      throw new Error("Invalid SAMI document");
    }

    return {
      metadata: {
        format: "smi",
        title,
      },
      cues,
    };
  },
};
