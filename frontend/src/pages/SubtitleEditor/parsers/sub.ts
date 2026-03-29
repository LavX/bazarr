import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";

const DEFAULT_FPS = 23.976;

function framesToMs(frame: number, fps: number): number {
  return Math.round((frame / fps) * 1000);
}

export const subParser: SubtitleParser = {
  parse(content: string): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "sub" }, cues: [] };
    }

    const lines = text.split("\n").filter((l) => l.trim());
    const cues = [];

    for (const line of lines) {
      const match = line.match(/^\{(\d+)\}\{(\d+)\}(.*)$/);
      if (!match) continue;

      const [, startFrame, endFrame, rawText] = match;
      const displayText = rawText.replace(/\|/g, "\n");

      cues.push({
        id: crypto.randomUUID(),
        startMs: framesToMs(parseInt(startFrame, 10), DEFAULT_FPS),
        endMs: framesToMs(parseInt(endFrame, 10), DEFAULT_FPS),
        text: displayText,
        rawText: line,
      });
    }

    return { metadata: { format: "sub" }, cues };
  },
};
