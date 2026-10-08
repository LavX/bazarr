/* eslint-disable camelcase */

import { PropsWithChildren } from "react";
import { QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useEpisodeAddBlacklist,
  useEpisodesHistory,
} from "@/apis/hooks/episodes";
import { JOB_WATCH_POLL_MS } from "@/apis/hooks/jobWatch";
import { useDownloadEpisodeSubtitles } from "@/apis/hooks/providers";
import { useSeriesAction } from "@/apis/hooks/series";
import {
  useBatchAction,
  useEpisodeSubtitleModification,
  useSubtitleAction,
} from "@/apis/hooks/subtitles";
import queryClient from "@/apis/queries";
import {
  EPISODES_HISTORY_DEBOUNCE_MS,
  invalidateEpisodeHistory,
} from "@/apis/queries/episodeHistory";
import { episodesHistoryKey, QueryKeys } from "@/apis/queries/keys";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import server from "@/tests/mocks/node";

const wrapper = ({ children }: PropsWithChildren) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

function emit(key: SocketIO.EventType, ids: number[] = []) {
  const reducer = createDefaultReducer().find((item) => item.key === key)!;
  reducer.any?.();
  (reducer.update as ((payload: number[]) => void) | undefined)?.(ids);
}

describe("series subtitle score history", () => {
  let requests: URL[];

  beforeEach(() => {
    requests = [];
    server.use(
      http.get("/api/episodes/history", ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          data: [{ series_id: Number(url.searchParams.get("series_id")) }],
          total: 1,
        });
      }),
    );
  });

  afterEach(() => vi.useRealTimers());

  it.each([undefined, 0, -1])(
    "does not request an invalid series id %s",
    (id) => {
      const { result } = renderHook(() => useEpisodesHistory(id), { wrapper });
      expect(result.current.fetchStatus).toBe("idle");
      expect(requests).toHaveLength(0);
    },
  );

  it("loads all history including embedded records with the local series id", async () => {
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(requests).toHaveLength(1);
    expect(requests[0].searchParams.get("series_id")).toBe("501");
    expect(requests[0].searchParams.get("include_embedded")).toBe("true");
    expect(requests[0].searchParams.get("length")).toBe("-1");
  });

  it("reuses fresh history for five minutes and then refreshes on remount", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    const { result: initialResult, unmount: unmountInitial } = renderHook(
      () => useEpisodesHistory(501),
      { wrapper },
    );
    await waitFor(() => expect(initialResult.current.isSuccess).toBe(true));
    unmountInitial();

    vi.setSystemTime(Date.now() + 4 * 60 * 1000);
    const { result: freshResult, unmount: unmountFresh } = renderHook(
      () => useEpisodesHistory(501),
      { wrapper },
    );
    await waitFor(() => expect(freshResult.current.isSuccess).toBe(true));
    expect(requests).toHaveLength(1);
    unmountFresh();

    vi.setSystemTime(Date.now() + 2 * 60 * 1000);
    const { result: staleResult } = renderHook(() => useEpisodesHistory(501), {
      wrapper,
    });
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(staleResult.current.isFetching).toBe(false));
  });

  it("ignores broad series events and another show's history writes", async () => {
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The socket refresh waits for the writes to stop, so the negative check
    // waits out that window first: a refresh that was delayed, not absent,
    // still lands within it.
    vi.useFakeTimers();
    await act(async () => {
      emit("series", [42]);
      emit("episode-history", [502]);
      emit("episode-history");
      await queryClient.invalidateQueries({ queryKey: [QueryKeys.Series] });
      await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS + 100);
    });
    vi.useRealTimers();
    expect(requests).toHaveLength(1);
    expect(
      queryClient.getQueryState(episodesHistoryKey(501))?.isInvalidated,
    ).toBe(false);

    await act(async () => emit("episode-history", [501]));
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
  });

  it("an episode update does not refetch the show history; the write does", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], {
      series_id: 501,
      sonarrSeriesId: 42,
    });
    queryClient.setQueryData([QueryKeys.Episodes, 9002], {
      series_id: 502,
      sonarrSeriesId: 42,
    });
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The score refresh comes from the history write, not from the episode
    // row changing, and it lands a moment after the writes stop. The negative
    // check waits out that window, so it catches a refresh that was delayed
    // rather than absent.
    vi.useFakeTimers();
    await act(async () => {
      emit("episode", [9001]);
      emit("episode", [9002]);
      emit("episode", [9999]);
      await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS + 100);
    });
    vi.useRealTimers();
    expect(requests).toHaveLength(1);
    expect(
      queryClient.getQueryState(episodesHistoryKey(501))?.isInvalidated,
    ).toBe(false);

    await act(async () => emit("episode-history", [501]));
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(
      requests.every((url) => url.searchParams.get("series_id") === "501"),
    ).toBe(true);
  });

  it("collapses a burst of history writes into one refresh", async () => {
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    await act(async () => {
      emit("episode-history", [501]);
      emit("episode-history", [501]);
      emit("episode-history", [501]);
    });
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(requests).toHaveLength(2);
  });

  it("does not show the previous show's history while the next show loads", async () => {
    let finishRequest!: () => void;
    const pending = new Promise<void>((resolve) => {
      finishRequest = resolve;
    });
    const { result, rerender } = renderHook(
      ({ id }) => useEpisodesHistory(id),
      { initialProps: { id: 501 }, wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    server.use(
      http.get("/api/episodes/history", async () => {
        await pending;
        return HttpResponse.json({ data: [{ series_id: 502 }], total: 1 });
      }),
    );

    rerender({ id: 502 });
    expect(result.current.data).toBeUndefined();
    finishRequest();
    await waitFor(() =>
      expect(result.current.data).toEqual([{ series_id: 502 }]),
    );
  });

  it.each(["upload", "socket"])(
    "restarts the first history request after a change from %s",
    async (source) => {
      queryClient.setQueryData([QueryKeys.Episodes, 9001], {
        series_id: 501,
        sonarrSeriesId: 42,
        sonarrEpisodeId: 100,
        arr_instance_id: 1,
      });
      let changed = false;
      let release!: () => void;
      const blocked = new Promise<void>((resolve) => {
        release = resolve;
      });
      server.use(
        http.get("/api/episodes/history", async ({ request }) => {
          const snapshot = changed
            ? { action: 4 }
            : { action: 1, score: "98.0%" };
          requests.push(new URL(request.url));
          if (requests.length === 1) await blocked;
          return HttpResponse.json({ data: [snapshot], total: 1 });
        }),
        http.post("/api/episodes/subtitles", () => {
          changed = true;
          return new HttpResponse(null, { status: 204 });
        }),
      );
      const { result } = renderHook(
        () => ({
          history: useEpisodesHistory(501),
          upload: useEpisodeSubtitleModification().upload,
        }),
        { wrapper },
      );
      await waitFor(() => expect(requests).toHaveLength(1));
      try {
        await act(async () => {
          if (source === "upload") {
            await result.current.upload.mutateAsync({
              seriesId: 42,
              episodeId: 100,
              arrInstanceId: 1,
              form: {
                language: "en",
                forced: false,
                hi: false,
                file: new File(["text"], "episode.en.srt"),
              },
            });
          } else {
            changed = true;
            emit("episode-history", [501]);
          }
        });
        await waitFor(() =>
          expect(result.current.history.data).toEqual([{ action: 4 }]),
        );
        expect(requests).toHaveLength(2);
      } finally {
        release();
      }
      await act(async () => {
        await blocked;
      });
      expect(result.current.history.data).toEqual([{ action: 4 }]);
      expect(
        queryClient.getQueryState(episodesHistoryKey(501))?.isInvalidated,
      ).toBe(false);
    },
  );

  it.each([undefined, null])(
    "refreshes history after Exclude with a legacy or empty job response %s",
    async (jobId) => {
      queryClient.setQueryData([QueryKeys.Episodes, 9001], {
        series_id: 501,
        sonarrSeriesId: 42,
        sonarrEpisodeId: 100,
        arr_instance_id: 1,
      });
      let changed = false;
      server.use(
        http.get("/api/episodes/history", ({ request }) => {
          requests.push(new URL(request.url));
          return HttpResponse.json({
            data: [changed ? { action: 0 } : { action: 1, score: "98.0%" }],
            total: 1,
          });
        }),
        http.post("/api/episodes/blacklist", () => {
          changed = true;
          return jobId === undefined
            ? new HttpResponse(null, { status: 200 })
            : HttpResponse.json({ job_id: jobId });
        }),
      );
      const { result } = renderHook(
        () => ({
          history: useEpisodesHistory(501),
          exclude: useEpisodeAddBlacklist(),
        }),
        { wrapper },
      );
      await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
      await act(async () => {
        await result.current.exclude.mutateAsync({
          seriesId: 42,
          episodeId: 100,
          form: {
            provider: "provider",
            subs_id: "subtitle",
            language: "en",
            subtitles_path: "/tv/episode.en.srt",
            arr_instance_id: 1,
          },
        });
      });
      await waitFor(() =>
        expect(result.current.history.data).toEqual([{ action: 0 }]),
      );
      expect(requests).toHaveLength(2);
    },
  );

  it.each(["completed", "failed"])(
    "refreshes only the Exclude owner's score when the replacement job is polled as %s",
    async (status) => {
      for (const [id, seriesId, instanceId] of [
        [9001, 501, 1],
        [9002, 502, 2],
      ]) {
        queryClient.setQueryData([QueryKeys.Episodes, id], {
          series_id: seriesId,
          sonarrSeriesId: 42,
          sonarrEpisodeId: 100,
          arr_instance_id: instanceId,
        });
      }
      let phase: "initial" | "deleted" | "replaced" = "initial";
      let polls = 0;
      server.use(
        http.get("/api/episodes/history", ({ request }) => {
          const url = new URL(request.url);
          requests.push(url);
          const changed = url.searchParams.get("series_id") === "501";
          return HttpResponse.json({
            data: [
              changed && phase === "deleted"
                ? { action: 0 }
                : {
                    action: 1,
                    score: changed && phase === "replaced" ? "72.0%" : "98.0%",
                  },
            ],
            total: 1,
          });
        }),
        http.post("/api/episodes/blacklist", () => {
          phase = "deleted";
          return HttpResponse.json({ job_id: 43 });
        }),
        http.get("/api/system/jobs", () => {
          polls++;
          if (polls === 2) phase = "replaced";
          return HttpResponse.json({
            data: [{ job_id: 43, status: polls === 1 ? "running" : status }],
          });
        }),
      );
      const { result } = renderHook(
        () => {
          const history = useEpisodesHistory(501);
          const otherHistory = useEpisodesHistory(502);
          return {
            history: history.data,
            otherHistory: otherHistory.data,
            isSuccess: history.isSuccess && otherHistory.isSuccess,
            exclude: useEpisodeAddBlacklist(),
          };
        },
        { wrapper },
      );
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      await act(async () => {
        const queued = await result.current.exclude.mutateAsync({
          seriesId: 42,
          episodeId: 100,
          form: {
            provider: "provider",
            subs_id: "subtitle",
            language: "en",
            subtitles_path: "/tv/episode.en.srt",
            arr_instance_id: 1,
          },
        });
        expect(queued).toEqual({ job_id: 43 });
      });
      expect(requests).toHaveLength(3);
      expect(queryClient.getQueryData(episodesHistoryKey(501))).toEqual([
        { action: 0 },
      ]);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS);
      });
      await vi.waitFor(() => expect(polls).toBe(1));
      expect(requests).toHaveLength(3);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS);
      });
      await vi.waitFor(() => expect(polls).toBe(2));
      vi.useRealTimers();
      await waitFor(() =>
        expect(result.current.history).toEqual([{ action: 1, score: "72.0%" }]),
      );
      expect(
        requests.slice(2).map((url) => url.searchParams.get("series_id")),
      ).toEqual(["501", "501"]);
      expect(result.current.otherHistory).toEqual([
        { action: 1, score: "98.0%" },
      ]);
    },
  );
  it("an episode batch does not refetch the show history", async () => {
    for (const id of [9001, 9002, 9003]) {
      queryClient.setQueryData([QueryKeys.Episodes, id], { series_id: 501 });
    }
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(async () => emit("episode", [9001, 9002, 9003]));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(requests).toHaveLength(1);
    expect(
      queryClient.getQueryState(episodesHistoryKey(501))?.isInvalidated,
    ).toBe(false);
  });

  it.each(["completed", "failed"])(
    "refreshes the queued download owner's history only when polling observes %s",
    async (status) => {
      for (const [id, seriesId, instanceId] of [
        [9001, 501, 1],
        [9002, 502, 2],
      ]) {
        queryClient.setQueryData([QueryKeys.Episodes, id], {
          series_id: seriesId,
          sonarrSeriesId: 42,
          sonarrEpisodeId: 100,
          arr_instance_id: instanceId,
        });
      }
      let completed = false;
      let polls = 0;
      server.use(
        http.get("/api/episodes/history", ({ request }) => {
          const url = new URL(request.url);
          requests.push(url);
          return HttpResponse.json({
            data: [
              {
                action: 1,
                score:
                  completed && url.searchParams.get("series_id") === "501"
                    ? "72.0%"
                    : "98.0%",
              },
            ],
            total: 1,
          });
        }),
        http.patch("/api/episodes/subtitles", () =>
          HttpResponse.json({ job_id: 73 }, { status: 202 }),
        ),
        http.get("/api/system/jobs", ({ request }) => {
          expect(new URL(request.url).searchParams.get("id")).toBe("73");
          polls++;
          completed = polls === 2;
          return HttpResponse.json({
            data: [{ job_id: 73, status: completed ? status : "running" }],
          });
        }),
      );
      const { result } = renderHook(
        () => {
          const history = useEpisodesHistory(501);
          const otherHistory = useEpisodesHistory(502);
          return {
            history: history.data,
            otherHistory: otherHistory.data,
            isSuccess: history.isSuccess && otherHistory.isSuccess,
            download: useEpisodeSubtitleModification().download,
          };
        },
        { wrapper },
      );
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      await act(async () => {
        expect(
          await result.current.download.mutateAsync({
            seriesId: 42,
            episodeId: 100,
            arrInstanceId: 1,
            form: { language: "en", forced: false, hi: false },
          }),
        ).toEqual({ job_id: 73 });
      });
      expect(requests).toHaveLength(2);
      await act(async () => vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS));
      await vi.waitFor(() => expect(polls).toBe(1));
      expect(requests).toHaveLength(2);
      await act(async () => vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS));
      await vi.waitFor(() => expect(polls).toBe(2));
      vi.useRealTimers();
      await waitFor(() =>
        expect(result.current.history).toEqual([{ action: 1, score: "72.0%" }]),
      );
      expect(requests).toHaveLength(3);
      expect(requests[2].searchParams.get("series_id")).toBe("501");
      expect(result.current.otherHistory).toEqual([
        { action: 1, score: "98.0%" },
      ]);
    },
  );

  it("observes the existing search returned by Exclude after deleting its subtitle", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], {
      series_id: 501,
      sonarrSeriesId: 42,
      sonarrEpisodeId: 100,
      arr_instance_id: 1,
    });
    queryClient.setQueryData(
      [QueryKeys.System, QueryKeys.Jobs],
      [{ job_id: 73, status: "running" }],
    );
    let phase = "initial";
    server.use(
      http.get("/api/episodes/history", ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({
          data: [
            phase === "deleted"
              ? { action: 0 }
              : {
                  action: 1,
                  score: phase === "completed" ? "72.0%" : "98.0%",
                },
          ],
          total: 1,
        });
      }),
      http.post("/api/episodes/blacklist", () => {
        phase = "deleted";
        return HttpResponse.json({ job_id: 73 });
      }),
    );
    const { result } = renderHook(
      () => {
        const history = useEpisodesHistory(501);
        return {
          data: history.data,
          isSuccess: history.isSuccess,
          exclude: useEpisodeAddBlacklist(),
        };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(async () => {
      await result.current.exclude.mutateAsync({
        seriesId: 42,
        episodeId: 100,
        form: {
          provider: "provider",
          subs_id: "subtitle",
          language: "en",
          subtitles_path: "/tv/episode.en.srt",
          arr_instance_id: 1,
        },
      });
    });
    await waitFor(() => expect(result.current.data).toEqual([{ action: 0 }]));
    await act(async () => {
      phase = "completed";
      queryClient.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        [{ job_id: 73, status: "completed" }],
      );
    });
    await waitFor(() =>
      expect(result.current.data).toEqual([{ action: 1, score: "72.0%" }]),
    );
    expect(requests).toHaveLength(3);
  });

  it("refreshes history after a manual download without Socket.IO", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], {
      series_id: 501,
      sonarrSeriesId: 42,
      sonarrEpisodeId: 100,
      arr_instance_id: 1,
    });
    server.use(
      http.post(
        "/api/providers/episodes",
        () => new HttpResponse(null, { status: 204 }),
      ),
    );
    const { result } = renderHook(
      () => ({
        history: useEpisodesHistory(501),
        download: useDownloadEpisodeSubtitles(),
      }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    await act(async () => {
      await result.current.download.mutateAsync({
        seriesId: 42,
        episodeId: 100,
        arrInstanceId: 1,
        form: {
          language: "en",
          hi: "False",
          forced: "False",
          original_format: "False",
          provider: "provider",
          subtitle: "subtitle-id",
        },
      });
    });
    expect(requests).toHaveLength(2);
    expect(requests[1].searchParams.get("series_id")).toBe("501");
  });

  it.each(
    [
      "manual",
      "translate",
      "sync",
      "remove_HI",
      "scan-disk",
      "search-missing",
    ].flatMap((operation) =>
      ["completed", "failed"].map((status) => ({ operation, status })),
    ),
  )(
    "refreshes only the owner of $operation when polling observes $status",
    async ({ operation, status }) => {
      for (const [id, seriesId, instanceId] of [
        [9001, 501, 1],
        [9002, 502, 2],
      ]) {
        queryClient.setQueryData([QueryKeys.Episodes, id], {
          series_id: seriesId,
          sonarrSeriesId: 42,
          sonarrEpisodeId: 100,
          arr_instance_id: instanceId,
        });
      }
      // The API can return an existing job, which was queued before this action.
      queryClient.setQueryData(
        [QueryKeys.System, QueryKeys.Jobs],
        [{ job_id: 73, status: "running" }],
      );
      let completed = false;
      let polls = 0;
      server.use(
        http.get("/api/episodes/history", ({ request }) => {
          const url = new URL(request.url);
          requests.push(url);
          const changed =
            completed && url.searchParams.get("series_id") === "501";
          return HttpResponse.json({
            data: [
              changed && operation === "translate"
                ? { action: 6 }
                : {
                    action: changed ? 2 : 1,
                    score: changed ? "72.0%" : "98.0%",
                  },
            ],
            total: 1,
          });
        }),
        http.post("/api/providers/episodes", () =>
          HttpResponse.json({ job_id: 73 }, { status: 202 }),
        ),
        http.patch("/api/subtitles", () =>
          HttpResponse.json({ job_id: 73 }, { status: 202 }),
        ),
        http.patch("/api/series", () =>
          HttpResponse.json({ job_id: 73 }, { status: 202 }),
        ),
        http.get("/api/system/jobs", ({ request }) => {
          expect(new URL(request.url).searchParams.get("id")).toBe("73");
          polls++;
          completed = polls === 2;
          return HttpResponse.json({
            data: [{ job_id: 73, status: completed ? status : "running" }],
          });
        }),
      );
      const { result } = renderHook(
        () => {
          const history = useEpisodesHistory(501);
          const otherHistory = useEpisodesHistory(502);
          return {
            history: history.data,
            otherHistory: otherHistory.data,
            isSuccess: history.isSuccess && otherHistory.isSuccess,
            manual: useDownloadEpisodeSubtitles(),
            tool: useSubtitleAction(),
            series: useSeriesAction(),
          };
        },
        { wrapper },
      );
      await waitFor(() => expect(result.current.isSuccess).toBe(true));
      vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
      await act(async () => {
        if (operation === "manual") {
          await result.current.manual.mutateAsync({
            seriesId: 42,
            episodeId: 100,
            arrInstanceId: 1,
            form: {
              language: "en",
              hi: "False",
              forced: "False",
              original_format: "False",
              provider: "provider",
              subtitle: "subtitle-id",
            },
          });
        } else if (
          operation === "scan-disk" ||
          operation === "search-missing"
        ) {
          await result.current.series.mutateAsync({
            action: operation,
            seriesid: 42,
            arr_instance_id: 1,
          });
        } else {
          await result.current.tool.mutateAsync({
            action: operation,
            form: {
              type: "episode",
              id: 100,
              arr_instance_id: 1,
              language: "en",
              from_language: "fr",
              path: "/tv/episode.fr.srt",
              hi: "False",
              forced: "False",
            },
          });
        }
      });
      expect(requests).toHaveLength(2);
      await act(async () => vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS));
      await vi.waitFor(() => expect(polls).toBe(1));
      expect(requests).toHaveLength(2);
      await act(async () => vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS));
      await vi.waitFor(() => expect(polls).toBe(2));
      vi.useRealTimers();
      await waitFor(() =>
        expect(result.current.history).toEqual(
          operation === "translate"
            ? [{ action: 6 }]
            : [{ action: 2, score: "72.0%" }],
        ),
      );
      expect(requests).toHaveLength(3);
      expect(requests[2].searchParams.get("series_id")).toBe("501");
      expect(result.current.otherHistory).toEqual([
        { action: 1, score: "98.0%" },
      ]);
    },
  );

  it.each([
    ["movie", QueryKeys.Movies],
    ["sports", QueryKeys.Sports],
  ] as const)(
    "keeps immediate %s refresh for existing mod jobs",
    async (mediaType, rootKey) => {
      const jobsKey = [QueryKeys.System, QueryKeys.Jobs];
      queryClient.setQueryData(jobsKey, [{ job_id: 73, status: "running" }]);
      queryClient.setQueryData([rootKey], []);
      server.use(
        http.patch("/api/subtitles", () =>
          HttpResponse.json({ job_id: 73 }, { status: 202 }),
        ),
      );
      const { result } = renderHook(() => useSubtitleAction(), { wrapper });
      try {
        await act(async () => {
          await result.current.mutateAsync({
            action: "remove_HI",
            form: {
              type: mediaType,
              id: 100,
              language: "en",
              path: "/media/video.en.srt",
            },
          });
        });
        expect(queryClient.getQueryState([rootKey])?.isInvalidated).toBe(true);
      } finally {
        // Settle a mistakenly registered watcher as well if the assertion fails.
        queryClient.setQueryData(jobsKey, [
          { job_id: 73, status: "completed" },
        ]);
      }
    },
  );

  it("refreshes Scan Disk history when the job finished before its HTTP response", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], {
      series_id: 501,
      sonarrSeriesId: 42,
      sonarrEpisodeId: 100,
      arr_instance_id: 1,
    });
    let completed = false;
    server.use(
      http.get("/api/episodes/history", ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({
          data: completed ? [{ action: 7, score: "100.0%" }] : [],
          total: completed ? 1 : 0,
        });
      }),
      http.patch("/api/series", () => {
        completed = true;
        queryClient.setQueryData(
          [QueryKeys.System, QueryKeys.Jobs],
          [{ job_id: 73, status: "completed" }],
        );
        return HttpResponse.json({ job_id: 73 }, { status: 202 });
      }),
    );
    const { result } = renderHook(
      () => {
        const history = useEpisodesHistory(501);
        return {
          data: history.data,
          isSuccess: history.isSuccess,
          scan: useSeriesAction(),
        };
      },
      { wrapper },
    );
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(async () => {
      await result.current.scan.mutateAsync({
        action: "scan-disk",
        seriesid: 42,
        arr_instance_id: 1,
      });
    });
    await waitFor(() =>
      expect(result.current.data).toEqual([{ action: 7, score: "100.0%" }]),
    );
    expect(requests).toHaveLength(2);
  });

  it("refreshes a batch's series once when completion is polled without Socket.IO", async () => {
    for (const [id, upstreamId] of [
      [9001, 100],
      [9002, 101],
    ]) {
      queryClient.setQueryData([QueryKeys.Episodes, id], {
        series_id: 501,
        sonarrSeriesId: 42,
        sonarrEpisodeId: upstreamId,
        arr_instance_id: 1,
      });
    }
    server.use(
      http.post("/api/subtitles/batch", () =>
        HttpResponse.json({ queued: 2, skipped: 0, errors: [], job_id: 31 }),
      ),
      http.get("/api/system/jobs", () =>
        HttpResponse.json({ data: [{ job_id: 31, status: "completed" }] }),
      ),
    );
    const { result } = renderHook(
      () => ({ history: useEpisodesHistory(501), batch: useBatchAction() }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.history.isSuccess).toBe(true));
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    await act(async () => {
      await result.current.batch.mutateAsync({
        items: [100, 101].map((id) => ({
          type: "episode" as const,
          sonarrSeriesId: 42,
          sonarrEpisodeId: id,
          arr_instance_id: 1,
        })),
        action: "translate",
      });
    });
    expect(requests).toHaveLength(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(JOB_WATCH_POLL_MS);
    });
    vi.useRealTimers();
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(result.current.history.isFetching).toBe(false));
    expect(requests[1].searchParams.get("series_id")).toBe("501");
  });

  it("coalesces episode and history events for one download", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], { series_id: 501 });
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    await act(async () => {
      emit("episode", [9001]);
      emit("episode-history", [501]);
    });
    // One download changes the episode row and writes one history row. The
    // row change never asks for the history; the write asks once, a moment
    // after the writes stop.
    await waitFor(() => expect(requests).toHaveLength(2));
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(requests).toHaveLength(2);
  });

  it("does not fetch twice when a refresh already covered the scheduled one", async () => {
    queryClient.setQueryData([QueryKeys.Episodes, 9001], {
      series_id: 501,
      sonarrSeriesId: 42,
      sonarrEpisodeId: 100,
      arr_instance_id: 1,
    });
    const { result } = renderHook(() => useEpisodesHistory(501), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));

    // The download's history write emits its socket event, which schedules
    // the show's refresh a moment out, and the mutation's own answer
    // refreshes the same show at once, through the path every mutation
    // takes. One fetch covers both.
    await act(async () => {
      emit("episode-history", [501]);
      await invalidateEpisodeHistory(queryClient, {
        episodeId: 100,
        seriesId: 42,
        arrInstanceId: 1,
      });
    });
    await waitFor(() => expect(result.current.isFetching).toBe(false));
    expect(requests).toHaveLength(2);

    // The scheduled refresh was covered by the one that ran, so waiting out
    // its window asks for the same history no second time.
    await act(async () => {
      await new Promise((resolve) =>
        setTimeout(resolve, EPISODES_HISTORY_DEBOUNCE_MS + 100),
      );
    });
    expect(requests).toHaveLength(2);
  });

  it.each([
    ["upload", 4],
    ["download", 1],
    ["remove", 0],
  ] as const)(
    "refreshes only the owning show's history after %s without Socket.IO",
    async (method, action) => {
      let changed = false;
      // The individual episode cache may have expired; the active series list
      // still identifies the owner. Another instance shares both upstream ids.
      queryClient.setQueryData(
        [QueryKeys.Series, 501, QueryKeys.Episodes, QueryKeys.All],
        [
          {
            id: 9001,
            series_id: 501,
            sonarrSeriesId: 42,
            sonarrEpisodeId: 100,
            arr_instance_id: 1,
          },
        ],
      );
      queryClient.setQueryData([QueryKeys.Episodes, 9002], {
        id: 9002,
        series_id: 502,
        sonarrSeriesId: 42,
        sonarrEpisodeId: 100,
        arr_instance_id: 2,
      });
      const modify = () => {
        changed = true;
        return new HttpResponse(null, { status: 204 });
      };
      server.use(
        http.get("/api/episodes/history", ({ request }) => {
          const url = new URL(request.url);
          requests.push(url);
          const modified =
            changed && url.searchParams.get("series_id") === "501";
          return HttpResponse.json({
            data: [
              {
                action: modified ? action : 1,
                score: modified ? "72.0%" : "98.0%",
              },
            ],
            total: 1,
          });
        }),
        http.post("/api/episodes/subtitles", modify),
        http.patch("/api/episodes/subtitles", modify),
        http.delete("/api/episodes/subtitles", modify),
      );
      const { result } = renderHook(
        () => ({
          history: useEpisodesHistory(501),
          otherHistory: useEpisodesHistory(502),
          modifications: useEpisodeSubtitleModification(),
        }),
        { wrapper },
      );
      await waitFor(() =>
        expect(
          result.current.history.isSuccess &&
            result.current.otherHistory.isSuccess,
        ).toBe(true),
      );
      expect(requests).toHaveLength(2);
      const form = {
        language: "en",
        forced: false,
        hi: false,
        path: "/tv/episode.en.srt",
        file: new File(["subtitle"], "episode.en.srt"),
      };
      await act(async () => {
        await result.current.modifications[method].mutateAsync({
          seriesId: 42,
          episodeId: 100,
          arrInstanceId: 1,
          form,
        });
      });
      await waitFor(() =>
        expect(result.current.history.data).toEqual([
          { action, score: "72.0%" },
        ]),
      );
      expect(result.current.otherHistory.data).toEqual([
        { action: 1, score: "98.0%" },
      ]);
      expect(
        requests.slice(2).map((url) => url.searchParams.get("series_id")),
      ).toEqual(["501"]);
    },
  );
});
