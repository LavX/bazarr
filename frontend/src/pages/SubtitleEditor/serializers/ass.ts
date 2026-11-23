import {
  ASS_EVENT_COLUMNS,
  type AssEventMetadata,
  assTextToDisplay,
  parseAssTimestamp,
  SSA_EVENT_COLUMNS,
} from "@/pages/SubtitleEditor/parsers/ass";
import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

const MINIMAL_HEADER = `[Script Info]
ScriptType: v4.00+
Title: Untitled

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1`;

function formatAssTimestamp(ms: number, raw?: string): string {
  if (raw !== undefined && parseAssTimestamp(raw) === ms) return raw;

  const clamped = Math.max(0, ms);
  const totalSeconds = Math.floor(clamped / 1000);
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;
  const centiseconds = Math.floor((clamped % 1000) / 10);

  return (
    String(hours) +
    ":" +
    String(minutes).padStart(2, "0") +
    ":" +
    String(seconds).padStart(2, "0") +
    "." +
    String(centiseconds).padStart(2, "0")
  );
}

export const assSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    const { metadata, cues } = result;

    const header = metadata.rawHeader ?? MINIMAL_HEADER;

    const fields = cues.map(
      (cue) => (cue.formatMetadata as AssEventMetadata | undefined)?.assFields,
    );
    const marked = fields.some((event) => event?.marked !== undefined);
    const columns = marked ? SSA_EVENT_COLUMNS : ASS_EVENT_COLUMNS;
    const marker = marked ? "Marked=0" : "0";
    const dialogueLines = cues.map((cue, index) => {
      const event = fields[index];
      const raw = event?.text;
      const text =
        raw !== undefined && cue.text === assTextToDisplay(raw)
          ? raw
          : cue.text.replace(/\n/g, "\\N");
      const values = [
        event?.marked ?? event?.layer ?? marker,
        formatAssTimestamp(cue.startMs, event?.start),
        formatAssTimestamp(cue.endMs, event?.end),
        event?.style ?? "Default",
        event?.name ?? "",
        event?.marginl ?? "0",
        event?.marginr ?? "0",
        event?.marginv ?? "0",
        event?.effect ?? "",
        text,
      ];
      return `Dialogue: ${values.join(",")}`;
    });

    return (
      header +
      "\n[Events]\nFormat: " +
      columns.join(", ") +
      "\n" +
      dialogueLines.join("\n")
    );
  },
};
