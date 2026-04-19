import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";
import { cueId } from "./uuid";

export const mplParser: SubtitleParser = {
  parse(content: string): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "mpl" }, cues: [] };
    }

    const lines = text.split("\n").filter((l) => l.trim());
    const cues = [];
    // MPL2 format: [start_ds][end_ds]text
    // ds = deciseconds (1/10 second)
    const pattern = /^\[(\d+)\]\[(\d+)\](.*)$/;

    for (const line of lines) {
      const match = line.match(pattern);
      if (!match) continue;

      const [, startDs, endDs, rawText] = match;
      const displayText = rawText.replace(/\|/g, "\n");

      cues.push({
        id: cueId(),
        startMs: parseInt(startDs, 10) * 100,
        endMs: parseInt(endDs, 10) * 100,
        text: displayText,
        rawText: line,
      });
    }

    return { metadata: { format: "mpl" }, cues };
  },
};
