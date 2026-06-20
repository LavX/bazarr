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
