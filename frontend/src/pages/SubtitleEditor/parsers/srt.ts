import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";
import { cueId } from "./uuid";

function parseTimestamp(raw: string): number | null {
  // HH:MM:SS,mmm or HH:MM:SS.mmm
  const match = raw.trim().match(/^(\d+):(\d+):(\d+)[,.](\d+)$/);
  if (!match) return null;
  const [, h, m, s, ms] = match;
  return (
    parseInt(h, 10) * 3600000 +
    parseInt(m, 10) * 60000 +
    parseInt(s, 10) * 1000 +
    parseInt(ms.padEnd(3, "0").slice(0, 3), 10)
  );
}

export const srtParser: SubtitleParser = {
  parse(content, { strict } = {}): ParseResult {
    // Normalize: strip BOM, normalize line endings
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "srt" }, cues: [] };
    }

    const blocks = text.split(/\n\n+/).filter((b) => b.trim());
    const cues = [];

    for (const block of blocks) {
      const lines = block.trim().split("\n");

      // Find the timestamp line. It could be line 0 (missing sequence number)
      // or line 1 (normal case).
      let tsLineIdx = -1;
      for (let i = 0; i < Math.min(lines.length, 2); i++) {
        if (lines[i].includes("-->")) {
          tsLineIdx = i;
          break;
        }
      }
      if (tsLineIdx === -1 || lines.length < 2) {
        if (strict) throw new Error("Missing SRT cue timing");
        continue;
      }

      const tsParts = lines[tsLineIdx].split("-->");
      const startMs = parseTimestamp(tsParts[0]);
      const endMs = parseTimestamp(tsParts[1].trim().split(/\s/)[0]);
      if (
        strict &&
        (tsParts.length !== 2 || startMs === null || endMs === null)
      ) {
        throw new Error("Invalid SRT cue timing");
      }
      const cueText = lines.slice(tsLineIdx + 1).join("\n");

      cues.push({
        id: cueId(),
        startMs: startMs ?? 0,
        endMs: endMs ?? 0,
        text: cueText,
      });
    }

    return { metadata: { format: "srt" }, cues };
  },
};
