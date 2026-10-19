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
