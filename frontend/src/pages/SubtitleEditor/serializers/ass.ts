import {
  ASS_EVENT_COLUMNS,
  type AssEventMetadata,
  assTextToDisplay,
} from "@/pages/SubtitleEditor/parsers/ass";
import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

const MINIMAL_HEADER = `[Script Info]
ScriptType: v4.00+
Title: Untitled

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,20,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,2,2,2,10,10,10,1`;

function formatAssTimestamp(ms: number): string {
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

/**
 * Patch an existing Dialogue line with updated timing and/or text.
 * Preserves all other fields (Layer, Style, Name, Margins, Effect).
 */
function patchDialogueLine(
  rawLine: string,
  startMs: number,
  endMs: number,
  text: string,
): string {
  // Dialogue: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
  const match = rawLine.match(/^(Dialogue:\s*)(.*)$/);
  if (!match) return rawLine;

  const afterPrefix = match[2];
  // Split into 10 fields (last field = Text, can contain commas)
  const parts: string[] = [];
  let remaining = afterPrefix;
  for (let i = 0; i < 9; i++) {
    const commaIdx = remaining.indexOf(",");
    if (commaIdx === -1) break;
    parts.push(remaining.slice(0, commaIdx));
    remaining = remaining.slice(commaIdx + 1);
  }
  parts.push(remaining); // Text field (10th)

  if (parts.length < 10) return rawLine; // Malformed, return as-is

  // Update Start (index 1), End (index 2), Text (index 9)
  parts[1] = formatAssTimestamp(startMs);
  parts[2] = formatAssTimestamp(endMs);
  parts[9] = text.replace(/\n/g, "\\N");

  return match[1] + parts.join(",");
}

export const assSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    const { metadata, cues } = result;

    const header = metadata.rawHeader ?? MINIMAL_HEADER;

    const fields = cues.map(
      (cue) => (cue.formatMetadata as AssEventMetadata | undefined)?.assFields,
    );
    const marked = fields.some((event) => event?.marked !== undefined);
    const columns = [
      marked ? "Marked" : ASS_EVENT_COLUMNS[0],
      ...ASS_EVENT_COLUMNS.slice(1),
    ];
    const marker = marked ? "Marked=0" : "0";
    const dialogueLines = cues.map((cue, index) => {
      const event = fields[index];
      if (event) {
        const raw = event.text;
        const edited = cue.text !== assTextToDisplay(raw);
        const values = [
          event.marked ?? event.layer ?? marker,
          formatAssTimestamp(cue.startMs),
          formatAssTimestamp(cue.endMs),
          event.style ?? "Default",
          event.name ?? "",
          event.marginl ?? "0",
          event.marginr ?? "0",
          event.marginv ?? "0",
          event.effect ?? "",
          edited ? cue.text.replace(/\n/g, "\\N") : raw,
        ];
        return `Dialogue: ${values.join(",")}`;
      }
      if (cue.rawText) {
        // Patch the raw line with any user edits to timing/text
        return patchDialogueLine(cue.rawText, cue.startMs, cue.endMs, cue.text);
      }
      // New cue: build from scratch
      const start = formatAssTimestamp(cue.startMs);
      const end = formatAssTimestamp(cue.endMs);
      const text = cue.text.replace(/\n/g, "\\N");
      return `Dialogue: ${marker},${start},${end},Default,,0,0,0,,${text}`;
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
