import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

function formatTxtTimestamp(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  return (
    String(hours).padStart(2, "0") +
    ":" +
    String(minutes).padStart(2, "0") +
    ":" +
    String(seconds).padStart(2, "0")
  );
}

export const txtSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    return result.cues
      .map((cue) => {
        const ts = formatTxtTimestamp(cue.startMs);
        const text = cue.text.replace(/\n/g, "|");
        return `${ts}:${text}`;
      })
      .join("\n");
  },
};
