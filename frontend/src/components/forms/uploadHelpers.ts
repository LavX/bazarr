// Pure helpers for the subtitle upload modals. Kept out of the components so
// the behaviours below are unit-tested directly. See Codex pass-3 on PR #248.

// Find the episode an embedded-info filename points at, or null.
export function matchEpisode(
  filename: string,
  infos: SubtitleInfo[],
  episodes: Item.Episode[],
): Item.Episode | null {
  const info = infos.find((v) => v.filename === filename);
  if (!info) {
    return null;
  }
  return (
    episodes.find(
      (v) => v.season === info.season && v.episode === info.episode,
    ) ?? null
  );
}

// Assign auto-matched episodes to rows that don't have one yet, PRESERVING any
// episode already chosen (manually, or by a prior match). Without the preserve
// guard, adding files re-runs matching and clobbers the user's manual choice.
export function assignEpisodes<
  T extends { file: { name: string }; episode: Item.Episode | null },
>(rows: T[], infos: SubtitleInfo[], episodes: Item.Episode[]): T[] {
  return rows.map((row) => {
    if (row.episode) {
      return row;
    }
    const matched = matchEpisode(row.file.name, infos, episodes);
    return matched ? { ...row, episode: matched } : row;
  });
}

// Whether the upload modal should auto-close: only once the initial load is
// ready, never mid-extraction, and only when there are genuinely no rows left.
export function shouldAutoCloseUpload(
  ready: boolean,
  processing: boolean,
  fileCount: number,
): boolean {
  return ready && !processing && fileCount <= 0;
}

// True when the chosen episode is one the opened series actually owns.
//
// Defence in depth. While the episode list is fetched with the correct series
// identifier this can never be false, but an identifier regression previously
// let another series' episode be auto-selected and uploaded with no warning,
// because submission only ever rejected an unset episode. Comparing on the
// upstream episode id rather than season/episode numbers matters: two series
// both have a season 1 episode 1.
export function episodeBelongsToSeries(
  episode: Item.Episode | null,
  episodes: Item.Episode[],
): boolean {
  if (episode === null) {
    return false;
  }
  return episodes.some((v) => v.sonarrEpisodeId === episode.sonarrEpisodeId);
}
