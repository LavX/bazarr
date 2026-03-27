import type { ParseResult } from "../types";
import type { SubtitleParser } from "./index";

function stripHtml(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<[^>]+>/g, "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&amp;/gi, "&")
    .trim();
}

export const smiParser: SubtitleParser = {
  parse(content: string): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "smi" }, cues: [] };
    }

    // Find all SYNC tags with their start times and collect text between them
    const syncPattern = /<SYNC\s+Start\s*=\s*(\d+)\s*>/gi;
    const syncs: { startMs: number; index: number }[] = [];
    let match: RegExpExecArray | null;

    while ((match = syncPattern.exec(text)) !== null) {
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
        id: crypto.randomUUID(),
        startMs: start.startMs,
        endMs,
        text: displayText,
      });
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
