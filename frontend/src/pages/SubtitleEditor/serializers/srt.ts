import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

function formatSrtTimestamp(ms: number): string {
  const totalSeconds = Math.floor(ms / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const milliseconds = ms % 1000;

  return (
    String(hours).padStart(2, "0") +
    ":" +
    String(minutes).padStart(2, "0") +
    ":" +
    String(seconds).padStart(2, "0") +
    "," +
    String(milliseconds).padStart(3, "0")
  );
}

export const srtSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    return result.cues
      .map((cue, i) => {
        const seq = i + 1;
        const start = formatSrtTimestamp(cue.startMs);
        const end = formatSrtTimestamp(cue.endMs);
        return `${seq}\n${start} --> ${end}\n${cue.text}`;
      })
      .join("\n\n");
  },
};
