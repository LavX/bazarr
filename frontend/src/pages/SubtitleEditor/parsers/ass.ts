import type { Cue, ParseResult } from "@/pages/SubtitleEditor/types";
import type { SubtitleParser } from "./index";
import { cueId } from "./uuid";

export function parseAssTimestamp(raw: string): number | null {
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

export function assTextToDisplay(text: string): string {
  let clean = stripOverrideTags(text);
  // \N and \n both become newlines for display
  clean = clean.replace(/\\N/g, "\n").replace(/\\n/g, "\n");
  return clean;
}

export const ASS_EVENT_COLUMNS = [
  "Layer",
  "Start",
  "End",
  "Style",
  "Name",
  "MarginL",
  "MarginR",
  "MarginV",
  "Effect",
  "Text",
] as const;

export const SSA_EVENT_COLUMNS = [
  "Marked",
  ...ASS_EVENT_COLUMNS.slice(1),
] as const;

export interface AssEventMetadata {
  assFields: Partial<Record<string, string>> & { text: string };
}

function formatColumns(value: string) {
  return value.split(",").map((column) => column.trim().toLowerCase());
}

function* dialogueRows(eventSections: string[][], strict: boolean) {
  const supported = new Set([
    "marked",
    ...ASS_EVENT_COLUMNS.map((column) => column.toLowerCase()),
  ]);
  let marker: string | undefined;
  for (const lines of eventSections) {
    const firstFormat = strict
      ? undefined
      : lines.find((line) => line.trim().startsWith("Format:"));
    let columns = firstFormat
      ? formatColumns(firstFormat.trim().replace(/^Format:\s*/, ""))
      : [];
    for (const line of lines) {
      if (strict && /^\s*Format\b/i.test(line)) {
        const declaration = line.match(/^\s*Format\s*:\s*(.*)$/i);
        columns = declaration ? formatColumns(declaration[1]) : [];
        if (
          !columns.includes("start") ||
          !columns.includes("end") ||
          columns.at(-1) !== "text" ||
          new Set(columns).size !== columns.length ||
          columns.some((column) => !supported.has(column)) ||
          (columns.includes("marked") && columns.includes("layer"))
        ) {
          throw new Error("Invalid ASS event format");
        }
        const declaredMarker = columns.find(
          (column) => column === "marked" || column === "layer",
        );
        if (marker && declaredMarker && marker !== declaredMarker) {
          throw new Error("Conflicting ASS event formats");
        }
        marker ??= declaredMarker;
        continue;
      }
      if (strict) {
        if (!/^\s*Dialogue\b/i.test(line)) continue;
        const dialogue = line.match(/^\s*Dialogue\s*:\s*([^\r\n]*)$/i);
        if (!dialogue || columns.length === 0) {
          throw new Error("Invalid ASS event format");
        }
        yield { columns, line, afterPrefix: dialogue[1] };
      } else if (line.trim().startsWith("Dialogue:")) {
        yield { columns, line, afterPrefix: line.replace(/^Dialogue:\s*/, "") };
      }
    }
  }
}

export const assParser: SubtitleParser = {
  parse(content, { strict } = {}): ParseResult {
    const text = content.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n");

    // A bare CR could hide another event within a line split on LF.
    if (strict && text.includes("\r")) {
      throw new Error("Invalid ASS line ending");
    }

    if (!text.trim()) {
      return { metadata: { format: "ass" }, cues: [] };
    }

    // Determine format variant
    const isSSA =
      !text.includes("[V4+ Styles]") && text.includes("[V4 Styles]");
    const format = isSSA ? "ssa" : "ass";

    // Keep strict Events sections separate so a declaration cannot carry over.
    const sections: Record<string, string> = {};
    const eventSections: string[][] = [];
    const lines = text.split("\n");
    let currentSection = "";
    let firstEventsLine: number | undefined;

    for (const [index, line] of lines.entries()) {
      const sectionMatch = strict
        ? line.match(/^\s*\[([^\]]+)\]\s*$/)
        : line.match(/^\[([^\]]+)\]/);
      if (sectionMatch) {
        currentSection = sectionMatch[1];
        if (strict && currentSection.trim().toLowerCase() === "events") {
          currentSection = "Events";
          firstEventsLine ??= index;
          eventSections.push([]);
        }
        sections[currentSection] = "";
      } else {
        if (strict) {
          if (/^\s*\[\s*Events\s*(?:\]|$)/i.test(line)) {
            throw new Error("Invalid ASS Events section");
          }
          if (currentSection === "Events") {
            eventSections.at(-1)?.push(line);
          } else if (/^\s*Dialogue\b/i.test(line)) {
            throw new Error("ASS dialogue is outside Events");
          }
        }
        if (currentSection) sections[currentSection] += line + "\n";
      }
    }

    const eventsIdx = text.indexOf("[Events]");
    const rawHeader =
      strict && firstEventsLine !== undefined
        ? lines.slice(0, firstEventsLine).join("\n")
        : eventsIdx >= 0
          ? text.slice(0, eventsIdx)
          : text;

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

    const events = strict
      ? eventSections
      : [(sections["Events"] ?? "").split("\n")];
    const cues: Cue[] = [];

    for (const { columns, line, afterPrefix } of dialogueRows(
      events,
      !!strict,
    )) {
      const textColIdx = columns.indexOf("text");
      const startColIdx = columns.indexOf("start");
      const endColIdx = columns.indexOf("end");
      // Split only up to (columns - 1) commas, because the Text field can contain commas
      const maxSplits = columns.length - 1;
      const parts: string[] = [];
      let remaining = afterPrefix;
      for (let i = 0; i < maxSplits; i++) {
        const commaIdx = remaining.indexOf(",");
        if (commaIdx === -1) break;
        parts.push(remaining.slice(0, commaIdx));
        remaining = remaining.slice(commaIdx + 1);
      }
      parts.push(remaining); // The rest is the Text column (or last column)

      if (strict && parts.length !== columns.length) {
        throw new Error("Invalid ASS cue fields");
      }

      const rawCueText =
        textColIdx >= 0 && textColIdx < parts.length - 1
          ? parts[textColIdx].trim()
          : parts[parts.length - 1];
      const startStr =
        startColIdx >= 0 && startColIdx < parts.length
          ? parts[startColIdx]
          : "";
      const endStr =
        endColIdx >= 0 && endColIdx < parts.length ? parts[endColIdx] : "";

      const startMs = parseAssTimestamp(startStr);
      const endMs = parseAssTimestamp(endStr);
      if (strict && (startMs === null || endMs === null)) {
        throw new Error("Invalid ASS cue timing");
      }
      cues.push({
        id: cueId(),
        startMs: startMs ?? 0,
        endMs: endMs ?? 0,
        text: assTextToDisplay(rawCueText),
        rawText: line,
        formatMetadata: {
          assFields: {
            // Fixed fields end before the unsplit remainder of a lenient row.
            ...Object.fromEntries(
              columns
                .slice(0, parts.length - 1)
                .map((column, index) => [column, parts[index]]),
            ),
            text: rawCueText,
          },
        } satisfies AssEventMetadata,
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
