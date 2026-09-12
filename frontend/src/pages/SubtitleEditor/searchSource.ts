import type { SearchSource } from "@/contexts/UniversalSearch";
import type { Cue } from "./types";

export function createSubtitleSearchSource(
  cues: Cue[],
  select: (index: number) => void,
): SearchSource {
  return {
    id: "open-subtitle",
    label: "In this subtitle",
    search(query) {
      const needle = query.trim().toLocaleLowerCase();
      if (!needle) return [];
      return cues
        .flatMap((cue, index) => {
          const position = cue.text.toLocaleLowerCase().indexOf(needle);
          if (position < 0) return [];
          const start = Math.max(0, position - 45);
          const seconds = Math.floor(cue.startMs / 1000);
          const timestamp = [
            Math.floor(seconds / 3600),
            Math.floor(seconds / 60) % 60,
            seconds % 60,
          ]
            .map((part) => String(part).padStart(2, "0"))
            .join(":");
          return [
            {
              id: cue.id,
              title: `${start ? "…" : ""}${cue.text.slice(start, start + 160)}${cue.text.length > start + 160 ? "…" : ""}`,
              detail: `${timestamp} · Cue ${index + 1}`,
              select: () => select(index),
            },
          ];
        })
        .slice(0, 20);
    },
  };
}
