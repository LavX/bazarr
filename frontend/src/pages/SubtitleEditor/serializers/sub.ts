import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

const FPS = 23.976;

function msToFrame(ms: number): number {
  return Math.round((ms * FPS) / 1000);
}

export const subSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    return result.cues
      .map((cue) => {
        if (cue.rawText) {
          return cue.rawText;
        }
        const startFrame = msToFrame(cue.startMs);
        const endFrame = msToFrame(cue.endMs);
        const text = cue.text.replace(/\n/g, "|");
        return `{${startFrame}}{${endFrame}}${text}`;
      })
      .join("\n");
  },
};
