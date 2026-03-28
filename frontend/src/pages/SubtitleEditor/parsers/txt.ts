import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";

function parseTimestampToMs(h: string, m: string, s: string): number {
  return (
    parseInt(h, 10) * 3600000 + parseInt(m, 10) * 60000 + parseInt(s, 10) * 1000
  );
}

export const txtParser: SubtitleParser = {
  parse(content: string): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "txt" }, cues: [] };
    }

    const lines = text.split("\n").filter((l) => l.trim());
    const cues = [];

    // Try TMPlayer format: HH:MM:SS:text or HH:MM:SS=text
    const tmPlayerPattern = /^(\d+):(\d+):(\d+)[:=](.+)$/;
    let isTmPlayer = false;

    // Check first non-empty line to detect format
    if (lines.length > 0 && tmPlayerPattern.test(lines[0])) {
      isTmPlayer = true;
    }

    if (isTmPlayer) {
      for (const line of lines) {
        const match = line.match(tmPlayerPattern);
        if (!match) continue;

        const [, h, m, s, cueText] = match;
        const startMs = parseTimestampToMs(h, m, s);
        const displayText = cueText.replace(/\|/g, "\n");

        cues.push({
          id: crypto.randomUUID(),
          startMs,
          endMs: startMs + 3000,
          text: displayText,
        });
      }

      // Adjust end times: each cue ends when the next one starts
      for (let i = 0; i < cues.length - 1; i++) {
        cues[i].endMs = cues[i + 1].startMs;
      }
    } else {
      // Try simple numbered format: N HH:MM:SS text
      const numberedPattern = /^(\d+)\s+(\d+):(\d+):(\d+)\s+(.+)$/;

      for (const line of lines) {
        const match = line.match(numberedPattern);
        if (!match) continue;

        const [, , h, m, s, cueText] = match;
        const startMs = parseTimestampToMs(h, m, s);

        cues.push({
          id: crypto.randomUUID(),
          startMs,
          endMs: startMs + 3000,
          text: cueText,
        });
      }

      // Adjust end times
      for (let i = 0; i < cues.length - 1; i++) {
        cues[i].endMs = cues[i + 1].startMs;
      }
    }

    return { metadata: { format: "txt" }, cues };
  },
};
