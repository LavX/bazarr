import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

function msToDeciseconds(ms: number): number {
  return Math.round(ms / 100);
}

export const mplSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    return result.cues
      .map((cue) => {
        if (cue.rawText) {
          return cue.rawText;
        }
        const startDs = msToDeciseconds(cue.startMs);
        const endDs = msToDeciseconds(cue.endMs);
        const text = cue.text.replace(/\n/g, "|");
        return `[${startDs}][${endDs}]${text}`;
      })
      .join("\n");
  },
};
