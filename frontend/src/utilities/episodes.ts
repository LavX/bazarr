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
 * The profile languages that apply to an episode with this audio, the way the
 * backend picks the languages it searches: a language whose rule excludes the
 * audio is left out, and so is one that is only for audio the episode does not
 * have. An item without the audio keys has no restriction.
 */
export function wantedLanguagesForAudio(
  items: {
    language: string;
    audio_exclude?: string;
    audio_only_include?: string;
  }[],
  audio: { code2: string }[],
): string[] {
  const audioCodes = new Set(audio.map((language) => language.code2));
  return items
    .filter((item) => {
      const matches = audioCodes.has(item.language);
      if (item.audio_exclude === "True" && matches) return false;
      if (item.audio_only_include === "True" && !matches) return false;
      return true;
    })
    .map((item) => item.language);
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
