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
  /**
   * Seerr takes per-season requests, the season list is known, and no season
   * is left to send: the non-4K lane has nothing to offer, whatever the media
   * row's `requestable` flag says. That flag only reports "not blocklisted",
   * so for a show it stays true long after every season is taken.
   */
  exhausted: boolean;
}

export function groupSeerrSeasons(
  title: MetadataTitle,
  state: SeerrMediaState,
): SeerrSeasonGroups {
  // hasList describes TMDB's list, not what survives the specials filter. A
  // show whose only entry is season 0, with specials off in Seerr, has a list
  // and nothing in it to request; folding those two into one flag told the
  // reader "Season details are unavailable" and then offered a whole-series
  // request that Seerr answers as a no-op.
  const listed: SeerrSeasonEntry[] =
    "seasons" in title && Array.isArray(title.seasons) ? title.seasons : [];
  const seasons = listed.filter((s) => state.special_episodes || s.season > 0);
  const seerr = new Map(state.seasons.map((s) => [s.number, s.state]));
  const ownedLocally = new Set(
    ("ownership" in title && title.ownership?.seasons_owned) || [],
  );
  const inSeerr = seasons.filter(
    (s) =>
      seerr.get(s.season) === "requested" ||
      seerr.get(s.season) === "available",
  );
  const rest = seasons.filter((s) => !inSeerr.includes(s));
  const owned = rest.filter((s) => ownedLocally.has(s.season));
  const open = rest.filter((s) => !ownedLocally.has(s.season));
  return {
    inSeerr,
    owned,
    open,
    hasList: listed.length > 0,
    exhausted:
      listed.length > 0 &&
      state.partial_requests &&
      owned.length === 0 &&
      open.length === 0,
  };
}
