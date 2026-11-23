import type { ParseResult, SubtitleFormat } from "@/pages/SubtitleEditor/types";
import { assParser } from "./ass";
import { mplParser } from "./mpl";
import { smiParser } from "./smi";
import { srtParser } from "./srt";
import { subParser } from "./sub";
import { txtParser } from "./txt";
import { vttParser } from "./vtt";

export interface SubtitleParser {
  parse(content: string, options?: { strict?: boolean }): ParseResult;
}

const parsers: Record<SubtitleFormat, SubtitleParser> = {
  srt: srtParser,
  ass: assParser,
  ssa: assParser,
  vtt: vttParser,
  sub: subParser,
  smi: smiParser,
  txt: txtParser,
  mpl: mplParser,
};

export function getParser(format: SubtitleFormat): SubtitleParser {
  return parsers[format];
}

// Conflict recovery adopts the entire file, so partial parses cannot replace
// the current draft. Ordinary open and import still use the lenient parsers.
export function parseReplacementSubtitle(
  format: SubtitleFormat,
  content: string,
) {
  const parsed = getParser(format).parse(content, { strict: true });
  if (parsed.cues.some((cue) => cue.endMs < cue.startMs)) {
    throw new Error("Subtitle cue ends before it starts");
  }
  return parsed;
}

const extensionMap: Record<string, SubtitleFormat> = {
  ".srt": "srt",
  ".ass": "ass",
  ".ssa": "ssa",
  ".vtt": "vtt",
  ".sub": "sub",
  ".smi": "smi",
  ".sami": "smi",
  ".txt": "txt",
  ".mpl": "mpl",
};

export function detectFormat(filename: string): SubtitleFormat {
  const dot = filename.lastIndexOf(".");
  if (dot === -1) return "srt";
  const ext = filename.slice(dot).toLowerCase();
  return extensionMap[ext] ?? "srt";
}
