import type { ParseResult, SubtitleFormat } from "@/pages/SubtitleEditor/types";
import { assSerializer } from "./ass";
import { mplSerializer } from "./mpl";
import { smiSerializer } from "./smi";
import { srtSerializer } from "./srt";
import { subSerializer } from "./sub";
import { txtSerializer } from "./txt";
import { vttSerializer } from "./vtt";

export interface SubtitleSerializer {
  serialize(result: ParseResult): string;
}

const serializers: Record<SubtitleFormat, SubtitleSerializer> = {
  srt: srtSerializer,
  ass: assSerializer,
  ssa: assSerializer,
  vtt: vttSerializer,
  sub: subSerializer,
  smi: smiSerializer,
  txt: txtSerializer,
  mpl: mplSerializer,
};

export function getSerializer(format: SubtitleFormat): SubtitleSerializer {
  return serializers[format];
}
