import type { ParseResult, SubtitleFormat } from "../types";
import { srtParser } from "./srt";
import { assParser } from "./ass";
import { vttParser } from "./vtt";
import { subParser } from "./sub";
import { smiParser } from "./smi";
import { txtParser } from "./txt";
import { mplParser } from "./mpl";

export interface SubtitleParser {
  parse(content: string): ParseResult;
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
