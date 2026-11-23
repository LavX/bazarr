import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  EPISODES_HISTORY_DEBOUNCE_MS,
  EPISODES_HISTORY_MAX_WAIT_MS,
} from "@/apis/queries/episodeHistory";
import { episodesHistoryKey, QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";

const queryClientMock = vi.hoisted(() => ({
  getQueryData: vi.fn(),
  getQueryState: vi.fn(),
  cancelQueries: vi.fn(),
  invalidateQueries: vi.fn(),
  setQueryData: vi.fn(),
}));

vi.mock("@/apis/queries", () => ({
  default: queryClientMock,
}));

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      jobs: vi.fn(),
    },
  },
}));

vi.mock("@/modules/notification", () => ({
  notification: {
    info: vi.fn(),
  },
}));

vi.mock("@mantine/notifications", () => ({
  showNotification: vi.fn(),
}));

vi.mock("@/utilities/console", () => ({
  LOG: vi.fn(),
}));

vi.mock("@/utilities/event", () => ({
  setOnlineStatus: vi.fn(),
}));

import { createDefaultReducer } from "./reducer";

function episodeReducer() {
  const reducer = createDefaultReducer().find(({ key }) => key === "episode");
  if (reducer === undefined) {
    throw new Error("episode reducer is missing");
  }
  return reducer;
}

function reducerFor(key: string) {
  const reducer = createDefaultReducer().find((item) => item.key === key);
  if (reducer === undefined) {
    throw new Error(`${key} reducer is missing`);
  }
  return reducer;
}

function emitEpisode(event: "update" | "delete", ids: number[]) {
  const handler = episodeReducer()[event] as
    | ((payload: number[]) => void)
    | undefined;
  handler?.(ids);
}

describe("socketio reducer", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  beforeEach(() => {
    queryClientMock.getQueryData.mockReset();
    queryClientMock.getQueryState.mockReset();
    queryClientMock.cancelQueries.mockReset();
    queryClientMock.invalidateQueries.mockReset();
    queryClientMock.setQueryData.mockReset();
  });

  it.each(["update", "delete"] as const)(
    "invalidates the local series query for episode %s events",
    async (event) => {
      vi.useFakeTimers();
      const localSeriesIdKey = "series_id";
      queryClientMock.getQueryData.mockReturnValue({
        [localSeriesIdKey]: 501,
        sonarrSeriesId: 42,
      });

      emitEpisode(event, [9001]);

      expect(queryClientMock.getQueryData).toHaveBeenCalledWith([
        QueryKeys.Episodes,
        9001,
      ]);
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, 501],
      });
      // The score history is not invalidated: episode rows change without
      // history rows changing, and history writes carry their own event. The
      // check waits out the window a scheduled or microtask refresh would
      // land in, so it catches one that was only delayed.
      await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS);
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, QueryKeys.Range, { scores: true }],
      });
      expect(queryClientMock.invalidateQueries).not.toHaveBeenCalledWith({
        queryKey: episodesHistoryKey(501),
        exact: true,
      });
      expect(queryClientMock.invalidateQueries).not.toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, 42],
      });
    },
  );

  it("falls back to the series list when an episode is not cached", async () => {
    vi.useFakeTimers();
    queryClientMock.getQueryData.mockReturnValue(undefined);

    emitEpisode("update", [9001]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series],
      predicate: expect.any(Function),
    });
    await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS);
  });

  it.each(["update", "delete"] as const)(
    "invalidates the wanted series query for episode-wanted %s events",
    (event) => {
      const handler = reducerFor("episode-wanted")[event] as
        | (() => void)
        | undefined;

      handler?.();

      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, QueryKeys.Wanted],
      });
    },
  );

  it("refreshes both Wanted lists and the navigation badge after a source save", () => {
    reducerFor("source-item").any?.();

    for (const kind of [QueryKeys.Series, QueryKeys.Movies]) {
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [kind, QueryKeys.Wanted],
        predicate: expect.any(Function),
      });
    }
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.System, QueryKeys.Badges],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.System, QueryKeys.Languages, "audio", true],
    });
  });

  it.each(["source-item", "source-group"] as const)(
    "refreshes source library and detail data after %s events",
    (event) => {
      reducerFor(event).any?.();
      for (const kind of [QueryKeys.Series, QueryKeys.Movies]) {
        const options = queryClientMock.invalidateQueries.mock.calls
          .map(([value]) => value)
          .find(
            (value) =>
              value.queryKey[0] === kind && value.queryKey[1] === "library",
          );
        expect(options).toBeDefined();
        expect(
          options.predicate({
            queryKey: [kind, "library", { includeSources: true }],
          }),
        ).toBe(true);
        expect(
          options.predicate({
            queryKey: [kind, "library", { includeSources: false }],
          }),
        ).toBe(false);
      }
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: ["source-detail"],
      });
    },
  );

  it("refreshes Sports activity containing source rows after a background source save", async () => {
    const update = reducerFor("source-sports").update as
      | ((payload: number[]) => void)
      | undefined;
    update?.([7]);
    await vi.waitFor(() =>
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.System, QueryKeys.Badges, true],
        exact: true,
      }),
    );
    const options = queryClientMock.invalidateQueries.mock.calls
      .map(([value]) => value)
      .find(({ queryKey }) => queryKey[0] === QueryKeys.Sports);
    expect(options).toBeDefined();
    for (const kind of ["leagues", "wanted", "history"]) {
      expect(
        options.predicate({
          queryKey: [QueryKeys.Sports, kind, { includeSources: true }],
        }),
      ).toBe(true);
      for (const [sourceKey, matches] of [
        ["source:7", true],
        ["source:8", false],
        ["arr:3", false],
      ] as const) {
        expect(
          options.predicate({
            queryKey: [
              QueryKeys.Sports,
              kind,
              { includeSources: true, sourceKeys: [sourceKey] },
            ],
          }),
        ).toBe(matches);
      }
      expect(
        options.predicate({
          queryKey: [
            QueryKeys.Sports,
            kind,
            { includeSources: true, owner: 3 },
          ],
        }),
      ).toBe(false);
      expect(
        options.predicate({
          queryKey: [QueryKeys.Sports, kind, { includeSources: false }],
        }),
      ).toBe(false);
    }
  });

  it("invalidates the wanted series query for reset-episode-wanted events", () => {
    reducerFor("reset-episode-wanted").any?.();

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series, QueryKeys.Wanted],
    });
  });

  it.each([
    ["movie", QueryKeys.Movies],
    ["series", QueryKeys.Series],
  ] as const)(
    "broadly invalidates %s queries on delete events",
    (key, queryKey) => {
      const handler = reducerFor(key).delete as
        | ((payload: number[]) => void)
        | undefined;
      handler?.([10]);

      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [queryKey, 10],
      });
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [queryKey],
      });
    },
  );

  it("schedules one trailing history refresh per show", async () => {
    vi.useFakeTimers();
    const handler = reducerFor("episode-history").update as
      | ((payload: number[]) => void)
      | undefined;

    handler?.([501, 502]);
    expect(queryClientMock.invalidateQueries).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS);
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: episodesHistoryKey(501),
      exact: true,
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: episodesHistoryKey(502),
      exact: true,
    });
  });

  it("collapses a burst of history writes for one show into one refresh", async () => {
    vi.useFakeTimers();
    const handler = reducerFor("episode-history").update as
      | ((payload: number[]) => void)
      | undefined;

    handler?.([501]);
    await vi.advanceTimersByTimeAsync(600);
    handler?.([501]);
    await vi.advanceTimersByTimeAsync(600);
    handler?.([501]);
    expect(queryClientMock.invalidateQueries).not.toHaveBeenCalled();

    await vi.advanceTimersByTimeAsync(EPISODES_HISTORY_DEBOUNCE_MS);
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledTimes(1);
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: episodesHistoryKey(501),
      exact: true,
    });
  });

  it("still refreshes on time while the writes keep coming", async () => {
    vi.useFakeTimers();
    const handler = reducerFor("episode-history").update as
      | ((payload: number[]) => void)
      | undefined;

    handler?.([501]);
    // Writes closer together than the wait would hold the refresh back
    // forever without its bound on the first write's wait.
    for (
      let elapsed = 0;
      elapsed < EPISODES_HISTORY_MAX_WAIT_MS;
      elapsed += 600
    ) {
      await vi.advanceTimersByTimeAsync(600);
      handler?.([501]);
    }
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledTimes(1);
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: episodesHistoryKey(501),
      exact: true,
    });
  });

  it("marks the jobs list stale when a finished job cannot be fetched", async () => {
    vi.mocked(api.system.jobs).mockRejectedValueOnce(new Error("offline"));
    const handler = reducerFor("jobs").update as (payload: unknown[]) => void;

    // eslint-disable-next-line camelcase
    handler([{ job_id: 7, status: "completed", progress_value: null }]);

    await vi.waitFor(() =>
      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.System, QueryKeys.Jobs],
        exact: true,
      }),
    );
    expect(queryClientMock.setQueryData).not.toHaveBeenCalled();
  });
});
