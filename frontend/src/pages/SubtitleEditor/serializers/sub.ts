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
        // A MicroDVD line is fully determined by start/end/text, so always
        // rebuild from the cue's current fields. Returning rawText verbatim
        // would discard any edits made to timing or text.
        const startFrame = msToFrame(cue.startMs);
        const endFrame = msToFrame(cue.endMs);
        const text = cue.text.replace(/\n/g, "|");
        return `{${startFrame}}{${endFrame}}${text}`;
      })
      .join("\n");
  },
};
