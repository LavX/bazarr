import type { Cue, ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";
import { cueId } from "./uuid";

function parseTimestamp(raw: string): number | null {
  // H:MM:SS.cc (centiseconds)
  const match = raw.trim().match(/^(\d+):(\d+):(\d+)\.(\d+)$/);
  if (!match) return null;
  const [, h, m, s, cs] = match;
  return (
    parseInt(h, 10) * 3600000 +
    parseInt(m, 10) * 60000 +
    parseInt(s, 10) * 1000 +
    parseInt(cs.padEnd(2, "0").slice(0, 2), 10) * 10
  );
}

function stripOverrideTags(text: string): string {
  // Remove {...} override blocks
  return text.replace(/\{[^}]*\}/g, "");
}

function assTextToDisplay(text: string): string {
  let clean = stripOverrideTags(text);
  // \N and \n both become newlines for display
  clean = clean.replace(/\\N/g, "\n").replace(/\\n/g, "\n");
  return clean;
}

export const assParser: SubtitleParser = {
  parse(content, { strict } = {}): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    if (!text.trim()) {
      return { metadata: { format: "ass" }, cues: [] };
    }

    // Determine format variant
    const isSSA =
      !text.includes("[V4+ Styles]") && text.includes("[V4 Styles]");
    const format = isSSA ? "ssa" : "ass";

    // Split into sections
    const sections: Record<string, string> = {};
    let currentSection = "";
    const lines = text.split("\n");

    for (const line of lines) {
      const sectionMatch = line.match(/^\[([^\]]+)\]/);
      if (sectionMatch) {
        currentSection = sectionMatch[1];
        sections[currentSection] = "";
      } else if (currentSection) {
        sections[currentSection] += line + "\n";
      }
    }

    // Build rawHeader from everything before [Events]
    const eventsIdx = text.indexOf("[Events]");
    const rawHeader = eventsIdx >= 0 ? text.slice(0, eventsIdx) : text;

    // Extract title from Script Info
    let title: string | undefined;
    const scriptInfo = sections["Script Info"] ?? "";
    const titleMatch = scriptInfo.match(/^Title:\s*(.+)$/m);
    if (titleMatch) {
      title = titleMatch[1].trim();
    }

    // Extract styles
    const stylesKey = isSSA ? "V4 Styles" : "V4+ Styles";
    const styles = sections[stylesKey]?.trim() || undefined;

    // Parse events
    const eventsText = sections["Events"] ?? "";
    const eventsLines = eventsText.split("\n").filter((l) => l.trim());

    // Find Format line to determine column order
    const formatLine = eventsLines.find((l) => l.trim().startsWith("Format:"));
    let formatColumns: string[] = [];
    if (formatLine) {
      formatColumns = formatLine
        .replace(/^Format:\s*/, "")
        .split(",")
        .map((c) => c.trim());
    }

    const textColIdx = formatColumns.findIndex(
      (c) => c.toLowerCase() === "text",
    );
    const startColIdx = formatColumns.findIndex(
      (c) => c.toLowerCase() === "start",
    );
    const endColIdx = formatColumns.findIndex((c) => c.toLowerCase() === "end");

    const cues: Cue[] = [];

    for (const line of eventsLines) {
      if (!line.trim().startsWith("Dialogue:")) continue;

      const afterPrefix = line.replace(/^Dialogue:\s*/, "");
      // Split only up to (columns - 1) commas, because the Text field can contain commas
      const maxSplits = formatColumns.length - 1;
      const parts: string[] = [];
      let remaining = afterPrefix;
      for (let i = 0; i < maxSplits; i++) {
        const commaIdx = remaining.indexOf(",");
        if (commaIdx === -1) break;
        parts.push(remaining.slice(0, commaIdx).trim());
        remaining = remaining.slice(commaIdx + 1);
      }
      parts.push(remaining); // The rest is the Text column (or last column)

      if (strict && textColIdx >= parts.length) {
        throw new Error("Invalid ASS cue fields");
      }

      const rawCueText =
        textColIdx >= 0 && textColIdx < parts.length
          ? parts[textColIdx]
          : parts[parts.length - 1];
      const startStr =
        startColIdx >= 0 && startColIdx < parts.length
          ? parts[startColIdx]
          : "";
      const endStr =
        endColIdx >= 0 && endColIdx < parts.length ? parts[endColIdx] : "";

      const startMs = parseTimestamp(startStr);
      const endMs = parseTimestamp(endStr);
      if (strict && (startMs === null || endMs === null)) {
        throw new Error("Invalid ASS cue timing");
      }
      cues.push({
        id: cueId(),
        startMs: startMs ?? 0,
        endMs: endMs ?? 0,
        text: assTextToDisplay(rawCueText),
        rawText: line,
      });
    }

    if (
      strict &&
      !cues.length &&
      (!/^\[(?:Script Info|V4\+? Styles|Events)\][ \t]*$/im.test(text.trim()) ||
        /^\s*Dialogue\s*:/im.test(text))
    ) {
      throw new Error("Invalid ASS document");
    }

    return {
      metadata: {
        format,
        title,
        styles,
        rawHeader: rawHeader.trimEnd(),
      },
      cues,
    };
  },
};
