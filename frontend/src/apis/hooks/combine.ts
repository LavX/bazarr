import { useMutation, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import client from "@/apis/raw/client";

type Scope =
  | { kind: "movie"; radarrId: number; arrInstanceId?: number }
  | { kind: "episode"; episodeId: number; arrInstanceId?: number }
  | { kind: "series"; seriesId: number; arrInstanceId?: number };

function buildPath(scope: Scope): string {
  switch (scope.kind) {
    case "movie":
      return `/movies/${scope.radarrId}/subtitles/combine`;
    case "episode":
      return `/episodes/${scope.episodeId}/subtitles/combine`;
    case "series":
      return `/series/${scope.seriesId}/subtitles/combine`;
  }
}

export function useCombineSubtitles() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: async (params: { scope: Scope; body: Api.CombineRequest }) => {
      const { scope, body } = params;
      const response = await client.axios.post<Api.CombineResult>(
        buildPath(scope),
        body,
        { params: { arr_instance_id: scope.arrInstanceId } },
      );
      return response.data;
    },
    onSuccess: (_data, variables) => {
      switch (variables.scope.kind) {
        case "movie":
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Movies, variables.scope.radarrId],
          });
          void qc.invalidateQueries({ queryKey: [QueryKeys.Movies] });
          break;
        case "episode":
          // The single-episode cache is keyed [Episodes, episodeId], but the
          // episodes table is keyed [Series, seriesId, Episodes, All], which the
          // former does not prefix-match, so the new combined subtitle stayed
          // invisible until a broader refetch. The episode scope carries no
          // seriesId, so invalidate the Series tree too; only the active series'
          // episode query refetches immediately, the rest just go stale.
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Episodes, variables.scope.episodeId],
          });
          void qc.invalidateQueries({ queryKey: [QueryKeys.Series] });
          break;
        case "series":
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Series, variables.scope.seriesId],
          });
          void qc.invalidateQueries({ queryKey: [QueryKeys.Series] });
          break;
      }
    },
  });
}
