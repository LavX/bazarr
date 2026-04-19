import type { ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleSerializer } from "./index";

export const smiSerializer: SubtitleSerializer = {
  serialize(result: ParseResult): string {
    const lines: string[] = ["<SAMI>", "<BODY>"];

    for (const cue of result.cues) {
      const text = cue.text.replace(/\n/g, "<br>");
      lines.push(`<SYNC Start=${cue.startMs}>`);
      lines.push(text);
      lines.push(`<SYNC Start=${cue.endMs}>`);
      lines.push("&nbsp;");
    }

    lines.push("</BODY>");
    lines.push("</SAMI>");

    return lines.join("\n");
  },
};
