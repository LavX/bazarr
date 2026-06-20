import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { BatchAction, BatchItem, BatchOptions } from "@/apis/raw/subtitles";

export function useSubtitleAction() {
  const client = useQueryClient();
  interface Param {
    action: string;
    form: FormType.ModifySubtitle;
  }
  return useMutation({
    mutationKey: [QueryKeys.Subtitles],
    mutationFn: (param: Param) =>
      api.subtitles.modify(param.action, param.form),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.History],
      });

      // TODO: Query less
      const { type, id } = param.form;
      if (type === "episode") {
        client.invalidateQueries({
          queryKey: [QueryKeys.Series, id],
        });
      } else {
        client.invalidateQueries({
          queryKey: [QueryKeys.Movies, id],
        });
      }
    },
  });
}

export function useEpisodeSubtitleModification() {
  const client = useQueryClient();

  interface Param<T> {
    seriesId: number;
    episodeId: number;
    // Owning Sonarr instance id (#156) so the action routes to the right server.
    arrInstanceId?: number;
    form: T;
  }

  const download = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Episodes],

    mutationFn: (param: Param<FormType.Subtitle>) =>
      api.episodes.downloadSubtitles(
        param.seriesId,
        param.episodeId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, param.seriesId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Series],
      });
    },
  });

  const remove = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Episodes],

    mutationFn: (param: Param<FormType.DeleteSubtitle>) =>
      api.episodes.deleteSubtitles(
        param.seriesId,
        param.episodeId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, param.seriesId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Series],
      });
    },
  });

  const upload = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Episodes],

    mutationFn: (param: Param<FormType.UploadSubtitle>) =>
      api.episodes.uploadSubtitles(
        param.seriesId,
        param.episodeId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, { seriesId }) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Series, seriesId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Series],
      });
    },
  });

  return { download, remove, upload };
}

export function useMovieSubtitleModification() {
  const client = useQueryClient();

  interface Param<T> {
    radarrId: number;
    // Owning Radarr instance id (#156) so the action routes to the right server.
    arrInstanceId?: number;
    form: T;
  }

  const download = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Movies],

    mutationFn: (param: Param<FormType.Subtitle>) =>
      api.movies.downloadSubtitles(
        param.radarrId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, param.radarrId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies],
      });
    },
  });

  const remove = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Movies],

    mutationFn: (param: Param<FormType.DeleteSubtitle>) =>
      api.movies.deleteSubtitles(
        param.radarrId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, param) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, param.radarrId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies],
      });
    },
  });

  const upload = useMutation({
    mutationKey: [QueryKeys.Subtitles, QueryKeys.Movies],

    mutationFn: (param: Param<FormType.UploadSubtitle>) =>
      api.movies.uploadSubtitles(
        param.radarrId,
        param.form,
        param.arrInstanceId,
      ),

    onSuccess: (_, { radarrId }) => {
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies, radarrId],
      });
      client.invalidateQueries({
        queryKey: [QueryKeys.Movies],
      });
    },
  });

  return { download, remove, upload };
}

export function useSubtitleInfos(names: string[]) {
  return useQuery({
    queryKey: [QueryKeys.Subtitles, QueryKeys.Infos, names],

    queryFn: () => api.subtitles.info(names),
    // Skip the request when there are no files yet (e.g. while an initial
    // archive selection is still being extracted).
    enabled: names.length > 0,
  });
}

export function useSubtitleContents(subtitlePath: string) {
  return useQuery({
    queryKey: [QueryKeys.Subtitles, subtitlePath],
    queryFn: () => api.subtitles.contents(subtitlePath),
    staleTime: Infinity,
  });
}

export function useSubtitleSyncStatus(
  mediaType: "episode" | "movie",
  mediaId: number | undefined,
  language: string,
  enabled: boolean,
  arrInstanceId?: number,
) {
  return useQuery({
    queryKey: [
      QueryKeys.Subtitles,
      "sync-status",
      mediaType,
      mediaId,
      language,
      arrInstanceId,
    ],
    queryFn: () =>
      api.subtitles.getSyncStatus(mediaType, mediaId!, language, arrInstanceId),
    enabled: enabled && mediaId !== undefined,
    refetchOnWindowFocus: false,
    refetchInterval: (query) => {
      const status = query.state.data?.jobStatus;
      return status === "pending" || status === "running" ? 2000 : false;
    },
  });
}

export function useRefTracksByEpisodeId(
  subtitlesPath: string,
  sonarrEpisodeId: number,
  isEpisode: boolean,
  arrInstanceId?: number,
) {
  return useQuery({
    queryKey: [
      QueryKeys.Episodes,
      sonarrEpisodeId,
      QueryKeys.Subtitles,
      subtitlesPath,
      arrInstanceId,
    ],
    queryFn: () =>
      api.subtitles.getRefTracksByEpisodeId(
        subtitlesPath,
        sonarrEpisodeId,
        arrInstanceId,
      ),
    enabled: isEpisode,
  });
}

export function useRefTracksByMovieId(
  subtitlesPath: string,
  radarrMovieId: number,
  isMovie: boolean,
  arrInstanceId?: number,
) {
  return useQuery({
    queryKey: [
      QueryKeys.Movies,
      radarrMovieId,
      QueryKeys.Subtitles,
      subtitlesPath,
      arrInstanceId,
    ],
    queryFn: () =>
      api.subtitles.getRefTracksByMovieId(
        subtitlesPath,
        radarrMovieId,
        arrInstanceId,
      ),
    enabled: isMovie,
  });
}

export function useUpgradableItems() {
  return useQuery({
    queryKey: [QueryKeys.Subtitles, "upgradable"],
    queryFn: () => api.subtitles.upgradable(),
    refetchInterval: 60000,
  });
}

export function useBatchAction() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.Subtitles, "batch"],
    mutationFn: (params: {
      items: BatchItem[];
      action: BatchAction;
      options?: BatchOptions;
    }) => api.subtitles.batch(params.items, params.action, params.options),
    onSuccess: () => {
      void client.invalidateQueries({
        queryKey: [QueryKeys.Series],
      });
      void client.invalidateQueries({
        queryKey: [QueryKeys.Movies],
      });
      void client.invalidateQueries({
        queryKey: [QueryKeys.History],
      });
      void client.invalidateQueries({
        queryKey: [QueryKeys.Translator],
      });
    },
  });
}

export function useSubtitleContent(
  mediaType: string | undefined,
  mediaId: number | undefined,
  language: string | undefined,
  arrInstanceId?: number,
) {
  return useQuery({
    queryKey: [
      QueryKeys.Subtitles,
      "content",
      mediaType,
      mediaId,
      language,
      arrInstanceId,
    ],
    queryFn: () => {
      if (!mediaType || mediaId === undefined || !language) {
        throw new Error("Missing parameters");
      }
      return api.subtitles.getContent(
        mediaType,
        mediaId,
        language,
        arrInstanceId,
      );
    },
    enabled: !!mediaType && mediaId !== undefined && !!language,
    staleTime: 5 * 60 * 1000,
    retry: (failureCount, error) => {
      // Don't retry 404s (subtitle doesn't exist yet)

      if (
        "response" in error &&
        (error as unknown as { response?: { status?: number } }).response
          ?.status === 404
      ) {
        return false;
      }
      return failureCount < 3;
    },
  });
}

export function useSubtitleSave() {
  return useMutation({
    mutationKey: [QueryKeys.Subtitles, "save"],
    mutationFn: (params: {
      mediaType: string;
      mediaId: number;
      language: string;
      content: string;
      encoding: string;
      etag?: string;
      arrInstanceId?: number;
    }) =>
      api.subtitles.saveContent(
        params.mediaType,
        params.mediaId,
        params.language,
        params.content,
        params.encoding,
        params.etag,
        params.arrInstanceId,
      ),
    onSuccess: () => {
      // Do NOT invalidate the content query here. The editor already has
      // the content (it just saved it) and the PUT response provides the
      // new ETag. Invalidating would trigger a refetch that races with
      // the ETag update and causes 412 on the next save.
    },
  });
}

export function usePromoteSyncSubtitle() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.Subtitles, "promote-sync-output"],
    mutationFn: (params: {
      mediaType: string;
      mediaId: number;
      targetLanguage: string;
      sourceLanguage: string;
      arrInstanceId?: number;
    }) =>
      api.subtitles.promoteSyncOutput(
        params.mediaType,
        params.mediaId,
        params.targetLanguage,
        params.sourceLanguage,
        params.arrInstanceId,
      ),
    onSuccess: (_, params) => {
      if (params.mediaType === "episode") {
        client.invalidateQueries({ queryKey: [QueryKeys.Series] });
      } else {
        client.invalidateQueries({ queryKey: [QueryKeys.Movies] });
      }
      client.invalidateQueries({ queryKey: [QueryKeys.History] });
      client.invalidateQueries({
        queryKey: [
          QueryKeys.Subtitles,
          "content",
          params.mediaType,
          params.mediaId,
          params.targetLanguage,
          params.arrInstanceId,
        ],
      });
    },
  });
}

export function useSubtitleCreate() {
  const client = useQueryClient();
  return useMutation({
    mutationKey: [QueryKeys.Subtitles, "create"],
    mutationFn: (params: {
      mediaType: string;
      mediaId: number;
      content: string;
      language: string;
      format: string;
      forced: boolean;
      hi: boolean;
      arrInstanceId?: number;
    }) =>
      api.subtitles.createSubtitle(
        params.mediaType,
        params.mediaId,
        params.content,
        params.language,
        params.format,
        params.forced,
        params.hi,
        params.arrInstanceId,
      ),
    onSuccess: (_, params) => {
      if (params.mediaType === "episode") {
        client.invalidateQueries({ queryKey: [QueryKeys.Series] });
      } else {
        client.invalidateQueries({ queryKey: [QueryKeys.Movies] });
      }
    },
  });
}
