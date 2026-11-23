import { useQuery } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

export default function useSourceOptions(
  kind: "movie" | "episode",
  enabled: boolean,
) {
  return useQuery({
    queryKey: [
      ...(kind === "movie"
        ? [QueryKeys.Movies]
        : [QueryKeys.Series, QueryKeys.Episodes]),
      QueryKeys.Blacklist,
      { sourceFilters: true },
    ],
    queryFn: ({ signal }) =>
      (kind === "movie" ? api.movies : api.episodes).blacklistSourceOptions(
        signal,
      ),
    enabled,
  });
}
