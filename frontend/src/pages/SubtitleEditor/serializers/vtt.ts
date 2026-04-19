import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

function formatVttTimestamp(ms: number): string {
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
    "." +
    String(milliseconds).padStart(3, "0")
  );
}

export const vttSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    const header = result.metadata.rawHeader ?? "WEBVTT";

    const cueBlocks = result.cues.map((cue) => {
      const start = formatVttTimestamp(cue.startMs);
      const end = formatVttTimestamp(cue.endMs);
      return `${start} --> ${end}\n${cue.text}`;
    });

    return header + "\n\n" + cueBlocks.join("\n\n");
  },
};
