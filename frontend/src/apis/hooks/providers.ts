import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { refreshWhenJobFinishes } from "@/apis/hooks/jobWatch";
import { invalidateEpisodeHistory } from "@/apis/queries/episodeHistory";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { searchAsJob } from "@/utilities/jobs";

export function useSystemProviders(history?: boolean) {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Providers, history ?? false],
    queryFn: () => api.providers.providers(history),
  });
}

export function useFlareSolverrStatus() {
  return useQuery({
    queryKey: [QueryKeys.System, QueryKeys.Providers, "flaresolverr"],
    queryFn: () => api.providers.flaresolverr(),
    retry: false,
    // The server answers from a cache and probes behind the request, so the
    // first read can say "still looking". Ask again until it has an answer.
    refetchInterval: (query) => (query.state.data?.detecting ? 2000 : false),
  });
}

export function useFlareSolverrDetect() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.providers.detectFlaresolverr(),
    onSuccess: async (status) => {
      const queryKey = [QueryKeys.System, QueryKeys.Providers, "flaresolverr"];
      // A status read still in flight was asked before this answer existed.
      // Left alone it would land afterwards and replace the newer result.
      await client.cancelQueries({ queryKey });
      client.setQueryData(queryKey, status);
    },
  });
}

/** Tests the address the person typed. It reports only and never writes the setting. */
export function useFlareSolverrTest() {
  return useMutation({
    mutationFn: (url: string) => api.providers.testFlaresolverr(url),
  });
}

// The manual searches run as standard jobs: the request queues one and the
// query follows it to its rows. Closing the modal does not stop the job, and
// pressing Search again for the same item follows the search that is running.
export function useMoviesProvider(radarrId?: number) {
  const client = useQueryClient();
  const result = useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Movies,
      radarrId,
    ],

    queryFn: () => {
      if (radarrId) {
        return searchAsJob<ManualSearchResponse>(client, () =>
          api.providers.searchMovie(radarrId),
        );
      }

      return { data: [], providers: [] };
    },

    staleTime: 0,
  });

  return {
    ...result,
    data: result.data?.data,
    providers: result.data?.providers,
  };
}

export function useEpisodesProvider(episodeId?: number) {
  const client = useQueryClient();
  const result = useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Episodes,
      episodeId,
    ],

    queryFn: () => {
      if (episodeId) {
        return searchAsJob<ManualSearchResponse>(client, () =>
          api.providers.searchEpisode(episodeId),
        );
      }

      return { data: [], providers: [] };
    },

    staleTime: 0,
  });

  return {
    ...result,
    data: result.data?.data,
    providers: result.data?.providers,
  };
}

export function useResetProvider() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.System, QueryKeys.Providers],
    mutationFn: () => api.providers.reset(),

    onSuccess: () => {
      client.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Providers],
      });
    },
  });
}

export function useDownloadEpisodeSubtitles() {
  const client = useQueryClient();

  return useMutation({
    mutationKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Subtitles,
      QueryKeys.Episodes,
    ],

    mutationFn: (param: {
      seriesId: number;
      episodeId: number;
      arrInstanceId?: number;
      form: FormType.ManualDownload;
    }) =>
      api.providers.downloadEpisodeSubtitle(
        param.seriesId,
        param.episodeId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (data, param) => {
      const refresh = () => {
        // Invalidate by prefix. Series queries are cached under the canonical
        // LOCAL series id, while seriesId here is the upstream one, so a key
        // built from it never matched a series cache entry.
        void client.invalidateQueries({
          queryKey: [QueryKeys.Series],
        });
        return invalidateEpisodeHistory(client, param);
      };
      return refreshWhenJobFinishes(client, data?.job_id, refresh);
    },
  });
}

export function useDownloadMovieSubtitles() {
  const client = useQueryClient();

  return useMutation({
    mutationKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Subtitles,
      QueryKeys.Movies,
    ],

    mutationFn: (param: {
      radarrId: number;
      arrInstanceId?: number;
      form: FormType.ManualDownload;
    }) =>
      api.providers.downloadMovieSubtitle(
        param.radarrId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: () => {
      // Invalidate by prefix. Movie queries are cached under the canonical
      // LOCAL id, while radarrId here is the upstream one, so a key built from
      // it never matched a movie cache entry.
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies],
      });
    },
  });
}
