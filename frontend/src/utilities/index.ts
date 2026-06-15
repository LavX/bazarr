import { Dispatch } from "react";
import { difference, differenceWith } from "lodash";
import { isEpisode, isMovie, isSeries } from "./validate";

export function toggleState(
  dispatch: Dispatch<boolean>,
  wait: number,
  start = false,
) {
  dispatch(!start);
  setTimeout(() => dispatch(start), wait);
}

export function GetItemId<T extends object>(item: T): number | undefined {
  // Prefer the canonical local id (#156); fall back to the upstream id for any
  // partially-typed payload. On a single default instance they are equal.
  if ("id" in item && typeof (item as { id?: number }).id === "number") {
    return (item as { id: number }).id;
  } else if (isMovie(item)) {
    return item.radarrId;
  } else if (isEpisode(item)) {
    return item.sonarrEpisodeId;
  } else if (isSeries(item)) {
    return item.sonarrSeriesId;
  } else {
    return undefined;
  }
}

export function BuildKey(...args: unknown[]) {
  return args.join("-");
}

export function Reload() {
  window.location.reload();
}

export function ScrollToTop() {
  window.scrollTo(0, 0);
}

const pathReplaceReg = new RegExp("/{1,}", "g");
export function pathJoin(...parts: string[]) {
  const separator = "/";
  return parts.join(separator).replace(pathReplaceReg, separator);
}

export function filterSubtitleBy(
  subtitles: Subtitle[],
  languages: Language.Info[],
): Subtitle[] {
  if (languages.length === 0) {
    return subtitles.filter((subtitle) => {
      return subtitle.path !== null;
    });
  } else {
    const result = differenceWith(
      subtitles,
      languages,
      (a, b) => a.code2 === b.code2 || a.path !== null || a.code2 === undefined,
    );
    return difference(subtitles, result);
  }
}

export function fromPython(value: PythonBoolean | undefined): boolean {
  return value === "True";
}

export function toPython(value: boolean): PythonBoolean {
  return value ? "True" : "False";
}

// Convert a job's progress value/max into a percentage clamped to 0-100.
// Defends the progress ring against backend value/max scale mismatches that
// could otherwise render nonsensical figures (e.g. 5967%).
export function progressPercent(value: number, max: number): number {
  if (!max || max <= 0) {
    return 0;
  }
  const percent = (value / max) * 100;
  if (!Number.isFinite(percent) || percent < 0) {
    return 0;
  }
  return Math.min(100, percent);
}

export * from "./env";
export * from "./hooks";
export * from "./validate";
