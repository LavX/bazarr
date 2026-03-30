import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";

function parseTimestamp(raw: string): number {
  // HH:MM:SS.mmm or MM:SS.mmm
  const match = raw.trim().match(/^(?:(\d+):)?(\d+):(\d+)\.(\d+)$/);
  if (!match) return 0;
  const [, h, m, s, ms] = match;
  return (
    parseInt(h ?? "0", 10) * 3600000 +
    parseInt(m, 10) * 60000 +
    parseInt(s, 10) * 1000 +
    parseInt(ms.padEnd(3, "0").slice(0, 3), 10)
  );
}

export const vttParser: SubtitleParser = {
  parse(content: string): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "vtt" }, cues: [] };
    }

    const lines = text.split("\n");

    // Verify WEBVTT header
    if (!lines[0].trim().startsWith("WEBVTT")) {
      return { metadata: { format: "vtt" }, cues: [] };
    }

    // Collect header metadata (STYLE, REGION blocks, NOTE blocks before first cue)
    let rawHeader = "";
    let i = 0;

    // Skip WEBVTT line and any following header text
    rawHeader += lines[i] + "\n";
    i++;

    // Collect everything before the first cue as header
    // A cue starts with a timestamp line containing -->
    const headerLines: string[] = [lines[0]];
    let foundFirstCue = false;
    for (i = 1; i < lines.length; i++) {
      if (lines[i].includes("-->")) {
        // Check if previous non-empty line was a cue identifier
        foundFirstCue = true;
        // Remove the cue ID line if it was added to header
        const lastNonEmpty = headerLines.length - 1;
        if (
          lastNonEmpty >= 0 &&
          headerLines[lastNonEmpty].trim() &&
          !headerLines[lastNonEmpty].includes("-->") &&
          !headerLines[lastNonEmpty].startsWith("STYLE") &&
          !headerLines[lastNonEmpty].startsWith("REGION") &&
          !headerLines[lastNonEmpty].startsWith("NOTE")
        ) {
          headerLines.pop();
        }
        break;
      }
      headerLines.push(lines[i]);
    }

    rawHeader = headerLines.join("\n").trimEnd();

    // Parse cue blocks
    const cues = [];
    const blocks = text.split(/\n\n+/);

    for (const block of blocks) {
      const blockLines = block.trim().split("\n");
      if (blockLines.length < 2) continue;

      // Find the timestamp line
      let tsLineIdx = -1;
      for (let j = 0; j < blockLines.length; j++) {
        if (blockLines[j].includes("-->")) {
          tsLineIdx = j;
          break;
        }
      }
      if (tsLineIdx === -1) continue;

      const tsLine = blockLines[tsLineIdx];
      // Split on --> but the right side may have positioning info
      const arrowIdx = tsLine.indexOf("-->");
      const leftTs = tsLine.slice(0, arrowIdx).trim();
      const rightPart = tsLine.slice(arrowIdx + 3).trim();
      // The end timestamp is the first token; the rest are settings
      const rightTokens = rightPart.split(/\s+/);
      const rightTs = rightTokens[0];

      const startMs = parseTimestamp(leftTs);
      const endMs = parseTimestamp(rightTs);
      const cueText = blockLines.slice(tsLineIdx + 1).join("\n");

      if (!cueText.trim() && startMs === 0 && endMs === 0) continue;

      cues.push({
        id: crypto.randomUUID(),
        startMs,
        endMs,
        text: cueText,
      });
    }

    // Extract styles from header
    let styles: string | undefined;
    if (foundFirstCue) {
      const styleMatch = rawHeader.match(/STYLE\s*\n([\s\S]*?)(?:\n\n|$)/);
      if (styleMatch) {
        styles = styleMatch[1].trim();
      }
    }

    return {
      metadata: {
        format: "vtt",
        styles: styles || undefined,
        rawHeader,
      },
      cues,
    };
  },
};
