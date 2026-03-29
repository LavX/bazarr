export type SubtitleFormat =
  | "srt"
  | "ass"
  | "ssa"
  | "vtt"
  | "sub"
  | "smi"
  | "txt"
  | "mpl";

export interface Cue {
  id: string;
  startMs: number;
  endMs: number;
  text: string;
  rawText?: string;
  formatMetadata?: unknown;
}

export interface SubtitleMetadata {
  format: SubtitleFormat;
  title?: string;
  styles?: unknown;
  rawHeader?: string;
}

export interface ParseResult {
  metadata: SubtitleMetadata;
  cues: Cue[];
}
