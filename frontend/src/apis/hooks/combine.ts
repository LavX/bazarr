import { useMutation, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import client from "@/apis/raw/client";

type Scope =
  | { kind: "movie"; radarrId: number }
  | { kind: "episode"; episodeId: number }
  | { kind: "series"; seriesId: number };

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
      );
      return response.data;
    },
    onSuccess: (_data, variables) => {
      switch (variables.scope.kind) {
        case "movie":
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Movies, variables.scope.radarrId],
          });
          break;
        case "episode":
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Episodes, variables.scope.episodeId],
          });
          break;
        case "series":
          void qc.invalidateQueries({
            queryKey: [QueryKeys.Series, variables.scope.seriesId],
          });
          break;
      }
    },
  });
}
