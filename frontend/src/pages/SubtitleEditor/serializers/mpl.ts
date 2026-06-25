import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

function msToDeciseconds(ms: number): number {
  return Math.round(ms / 100);
}

export const mplSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    return result.cues
      .map((cue) => {
        // An MPL2 line is fully determined by start/end/text, so always
        // rebuild from the cue's current fields. Returning rawText verbatim
        // would discard any edits made to timing or text.
        const startDs = msToDeciseconds(cue.startMs);
        const endDs = msToDeciseconds(cue.endMs);
        const text = cue.text.replace(/\n/g, "|");
        return `[${startDs}][${endDs}]${text}`;
      })
      .join("\n");
  },
};
