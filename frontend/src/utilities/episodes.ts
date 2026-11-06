/** An episode the way Sonarr and most players name it: S01E05. */
export function episodeCode(
  season: number | string,
  episode: number | string,
): string {
  return `S${String(season).padStart(2, "0")}E${String(episode).padStart(2, "0")}`;
}

/**
 * The same name for an episode the history API sends as "1x5". Anything
 * else comes back as it came.
 */
export function episodeCodeOf(value: string): string {
  const match = /^(\d+)x(\d+)$/.exec(value.trim());
  return match ? episodeCode(match[1], match[2]) : value;
}

/**
 * Whether the row offers "Force duplicate search": the setting that counts
 * embedded tracks as downloaded is on, and the episode has an embedded track in
 * a wanted language, shown or hidden. With the setting off the wanted search
 * already looks for the missing external subtitle, so the action would change
 * nothing. A hidden track is not in the episode's subtitles, so it comes from
 * the hidden-track list.
 */
export function canForceDuplicateSearch({
  embeddedAsDownloaded,
  wantedLanguages,
  subtitles,
  hiddenLanguages,
}: {
  embeddedAsDownloaded: boolean;
  wantedLanguages: string[];
  subtitles: { code2: string; path?: string | null }[];
  hiddenLanguages: string[];
}): boolean {
  if (!embeddedAsDownloaded) return false;
  const wanted = new Set(wantedLanguages);
  return (
    subtitles.some((sub) => !sub.path && wanted.has(sub.code2)) ||
    hiddenLanguages.some((code2) => wanted.has(code2))
  );
}
