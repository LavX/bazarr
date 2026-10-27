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

// The manual searches run as standard jobs: the request queues one and the
// query follows it to its rows. Closing the modal does not stop the job, and
// pressing Search again for the same item follows the search that is running.
export function useMoviesProvider(radarrId?: number) {
  const client = useQueryClient();
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Movies,
      radarrId,
    ],

    queryFn: () => {
      if (radarrId) {
        return searchAsJob<SearchResultType[]>(client, () =>
          api.providers.searchMovie(radarrId),
        );
      }

      return [];
    },

    staleTime: 0,
  });
}

export function useEpisodesProvider(episodeId?: number) {
  const client = useQueryClient();
  return useQuery({
    queryKey: [
      QueryKeys.System,
      QueryKeys.Providers,
      QueryKeys.Episodes,
      episodeId,
    ],

    queryFn: () => {
      if (episodeId) {
        return searchAsJob<SearchResultType[]>(client, () =>
          api.providers.searchEpisode(episodeId),
        );
      }

      return [];
    },

    staleTime: 0,
  });
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
