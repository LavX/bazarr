// What Seerr knows about the selected title, as the availability row needs it.
//
// The row states where a title stands once, as one sentence: the library chip
// is the subject and Seerr's state is its second clause. So this module owns
// the identity a title maps to, and the clause the chip appends. SeerrAction
// renders the actions from the same query; both call useSeerrMedia with the
// same key, so React Query serves one request to both.
import { useMemo } from "react";
import { useSeerrMedia } from "@/apis/hooks/seerr";
import { useSystemSettings } from "@/apis/hooks/system";
import type { MetadataTitle } from "@/types/discover";
import type { SeerrIdentity, SeerrMediaState } from "@/types/seerr";

export function seerrIdentity(
  title: MetadataTitle | null,
): SeerrIdentity | null {
  if (!title) return null;
  if (title.source === "tmdb" && typeof title.id === "number") {
    return title.media_type === "show"
      ? { kind: "tv", tmdbId: title.id }
      : { kind: "movie", tmdbId: title.id };
  }
  if (
    title.source === "local" &&
    title.media_type === "show" &&
    typeof title.tvdb_id === "number"
  ) {
    return { kind: "tv", tvdbId: title.tvdb_id };
  }
  if (
    title.source === "local" &&
    title.media_type === "movie" &&
    typeof title.tmdb_id === "number" &&
    title.tmdb_id > 0
  ) {
    return { kind: "movie", tmdbId: title.tmdb_id };
  }
  return null;
}

// Two peer facts joined by the same bullet the meta line above the chip uses
// for "2026 · Film", so each is capitalised the way that line's halves are:
// where the title stands with this library, and where it stands in Seerr.
const CLAUSES: Record<string, string> = {
  pending: "Awaiting approval in Seerr",
  processing: "Processing in Seerr",
  declined: "Declined in Seerr",
  failed: "Failed in Seerr",
  partially_available: "Some seasons available in Seerr",
  available: "Ready in Seerr",
  blocklisted: "Blocklisted in Seerr",
};

/**
 * The clause the library chip appends, or null when Seerr adds nothing.
 *
 * Order matters, and APPROVED is the whole reason. Seerr leaves a finished
 * request at APPROVED rather than tidying it away, so reading that before the
 * media row reported "Processing" over a title already available, and hid
 * "Some seasons available" behind the stale row. Only that one check sits
 * below the media row. A request still pending, declined or failed stays
 * above it: a series Seerr holds in full can have a brand new season
 * requested, and this clause is the only place the reader learns what became
 * of it.
 */
export function seerrClause(
  state: SeerrMediaState,
  inLibrary: boolean,
): string | null {
  if (state.request?.status === "pending") return CLAUSES.pending;
  if (state.request?.status === "declined") return CLAUSES.declined;
  if (state.request?.status === "failed") return CLAUSES.failed;
  // "Ready in Seerr" next to "In your library" is the same fact twice, which
  // is the repetition this clause exists to remove. Next to "Not in your
  // library" it is a genuine disagreement worth reading.
  if (state.status === "available") return inLibrary ? null : CLAUSES.available;
  if (state.status === "blocklisted") return CLAUSES.blocklisted;
  if (state.status === "partially_available")
    return CLAUSES.partially_available;
  if (state.status === "processing" || state.request?.status === "approved")
    return CLAUSES.processing;
  if (state.status === "pending") return CLAUSES.pending;
  // Only "unknown" and "deleted" reach here, and neither is worth a clause:
  // "unknown" is the ordinary case of a title Seerr has never been asked for.
  return null;
}

/** The chip's Seerr clause for this title, or null while it cannot be known. */
export function useSeerrClause(
  title: MetadataTitle | null,
  inLibrary: boolean,
): string | null {
  const { data: settings } = useSystemSettings();
  const enabled = settings?.general?.use_seerr === true;
  const identity = useMemo(
    () => (enabled ? seerrIdentity(title) : null),
    [enabled, title],
  );
  const { data } = useSeerrMedia(identity, enabled && identity !== null);
  if (!data || !("configured" in data) || "error_code" in data) return null;
  return seerrClause(data, inLibrary);
}
