// The season-grouping arithmetic behind both the "Request seasons" entry
// point (SeerrAction) and the seasons flow (SeerrRequestModal). It exists
// exactly once so the button and the modal can never disagree about which
// seasons are already in Seerr, owned locally, or open to request.
import type { MetadataTitle } from "@/types/discover";
import type { SeerrMediaState } from "@/types/seerr";

export interface SeerrSeasonEntry {
  season: number;
  title: string;
}

export interface SeerrSeasonGroups {
  /** Seasons Seerr already has requested or available: read-only in the UI. */
  inSeerr: SeerrSeasonEntry[];
  /** Seasons Bazarr+ already owns a copy of, and Seerr does not yet have. */
  owned: SeerrSeasonEntry[];
  /** Seasons open to request: neither in Seerr nor owned locally. */
  open: SeerrSeasonEntry[];
  /** Whether TMDB's own season list was available to group at all. */
  hasList: boolean;
}

export function groupSeerrSeasons(
  title: MetadataTitle,
  state: SeerrMediaState,
): SeerrSeasonGroups {
  const seasons: SeerrSeasonEntry[] = (
    "seasons" in title && Array.isArray(title.seasons) ? title.seasons : []
  ).filter((s) => state.special_episodes || s.season > 0);
  const seerr = new Map(state.seasons.map((s) => [s.number, s.state]));
  const owned = new Set(
    ("ownership" in title && title.ownership?.seasons_owned) || [],
  );
  const inSeerr = seasons.filter(
    (s) =>
      seerr.get(s.season) === "requested" ||
      seerr.get(s.season) === "available",
  );
  const rest = seasons.filter((s) => !inSeerr.includes(s));
  return {
    inSeerr,
    owned: rest.filter((s) => owned.has(s.season)),
    open: rest.filter((s) => !owned.has(s.season)),
    hasList: seasons.length > 0,
  };
}
