import { Query, useQueries, useQuery } from "@tanstack/react-query";
import api from "@/apis/raw";
import type {
  LinkMediaType,
  MediaServerLinkAnswer,
  MediaServerLinkTarget,
} from "@/apis/raw/mediaServerLinks";

// Under the media server keys, so a saved destination refreshes these too.
export const mediaServerLinksKey = ["media-servers", "links"];
// The server keeps a found or missing item for fifteen minutes; so does the
// page. It never keeps a server that did not answer, and neither does this:
// that answer is stale at once, so the next open asks again.
const ANSWER_MS = 15 * 60 * 1000;

export function answerStaleTime(query: Query<MediaServerLinkAnswer>) {
  return query.state.status === "success" &&
    query.state.data?.state !== "unreachable"
    ? ANSWER_MS
    : 0;
}

/** Every destination a link can open. One request per page, shared. */
export function useMediaServerLinkDestinations() {
  return useQuery({
    queryKey: [...mediaServerLinksKey, "destinations"],
    queryFn: () => api.mediaServerLinks.destinations(),
    staleTime: 60 * 1000,
    retry: false,
  });
}

/** The destinations that can hold one row. No server is asked for these. */
export function useMediaServerLinkCandidates(
  mediaType: LinkMediaType,
  id: number,
  enabled: boolean,
) {
  return useQuery({
    queryKey: [...mediaServerLinksKey, "candidates", mediaType, id],
    queryFn: () => api.mediaServerLinks.candidates(mediaType, id),
    enabled,
    staleTime: ANSWER_MS,
    retry: false,
    refetchOnWindowFocus: false,
  });
}

/** One answer per destination, each on its own request and its own time. */
export function useMediaServerLinkAnswers(
  targets: MediaServerLinkTarget[],
  mediaType: LinkMediaType,
  id: number,
  enabled: boolean,
) {
  return useQueries({
    queries: targets.map((target) => ({
      queryKey: [
        ...mediaServerLinksKey,
        "resolve",
        target.instance_id,
        mediaType,
        id,
      ],
      queryFn: () =>
        api.mediaServerLinks.resolve(target.instance_id, mediaType, id),
      enabled,
      staleTime: answerStaleTime,
      gcTime: ANSWER_MS,
      retry: false,
      refetchOnWindowFocus: false,
    })),
  });
}
