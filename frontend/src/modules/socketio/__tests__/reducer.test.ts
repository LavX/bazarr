/**
 * Behavior tests for createDefaultReducer().
 *
 * Covers every reducer key: verifies that socket events trigger the correct
 * queryClient.invalidateQueries calls (with exact query keys).
 *
 * The "episode" reducer (local-id cache lookup + series fallback) is already
 * exercised by the sibling reducer.test.ts; here we focus on the gaps:
 * inline jobs cache mutation, all "any" key reducers and the
 * connect/disconnect lifecycle.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";

// ---------------------------------------------------------------------------
// Hoist mocks so vi.mock factories can reference them
// ---------------------------------------------------------------------------

const queryClientMock = vi.hoisted(() => ({
  getQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
  resetQueries: vi.fn(),
  setQueryData: vi.fn(),
}));

const showNotificationMock = vi.hoisted(() => vi.fn());
const updateNotificationMock = vi.hoisted(() => vi.fn());
const hideNotificationMock = vi.hoisted(() => vi.fn());
const setOnlineStatusMock = vi.hoisted(() => vi.fn());
const logMock = vi.hoisted(() => vi.fn());

vi.mock("@/apis/queries", () => ({ default: queryClientMock }));

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      jobs: vi.fn().mockResolvedValue([]),
    },
  },
}));

vi.mock("@mantine/notifications", () => ({
  showNotification: showNotificationMock,
  updateNotification: updateNotificationMock,
  hideNotification: hideNotificationMock,
}));

vi.mock("@/utilities/console", () => ({ LOG: logMock }));

vi.mock("@/utilities/event", () => ({ setOnlineStatus: setOnlineStatusMock }));

import { createDefaultReducer } from "@/modules/socketio/reducer";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Return the reducer for the given socket event key. */
function get(key: string) {
  const r = createDefaultReducer().find((item) => item.key === key);
  if (r === undefined) throw new Error(`reducer for key "${key}" not found`);
  return r;
}

/** Invoke the reducer's "any" handler. */
function any(key: string) {
  get(key).any?.();
}

/** Invoke the reducer's "update" handler. */
function update<T>(key: string, payload: T[]) {
  (get(key).update as ((p: T[]) => void) | undefined)?.(payload);
}

/** Invoke the reducer's "delete" handler. */
function del<T>(key: string, payload: T[]) {
  (get(key).delete as ((p: T[]) => void) | undefined)?.(payload);
}

// ---------------------------------------------------------------------------
// Test suite
// ---------------------------------------------------------------------------

beforeEach(() => {
  vi.clearAllMocks();
});

// ---- Lifecycle (connect / disconnect / connect_error) ---------------------

describe("lifecycle reducers", () => {
  it("sets online status to true on connect", () => {
    any("connect");
    expect(setOnlineStatusMock).toHaveBeenCalledWith(true);
  });

  it("sets online status to false on disconnect", () => {
    any("disconnect");
    expect(setOnlineStatusMock).toHaveBeenCalledWith(false);
  });

  it("sets online status to false on connect_error", () => {
    any("connect_error");
    expect(setOnlineStatusMock).toHaveBeenCalledWith(false);
  });
});

// ---- series reducer -------------------------------------------------------

describe("series reducer", () => {
  it("update: invalidates each series by id and the series list", () => {
    update("series", [10, 20]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series, 10],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series, 20],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series],
    });
  });

  it("delete: invalidates each series by id and the series list", () => {
    del("series", [99]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series, 99],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series],
    });
  });
});

// ---- movie reducer --------------------------------------------------------

describe("movie reducer", () => {
  it("update: invalidates each movie by id and the movies list", () => {
    update("movie", [5, 6]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies, 5],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies, 6],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies],
    });
  });

  it("delete: invalidates each movie by id and the movies list", () => {
    del("movie", [77]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies, 77],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Movies],
    });
  });
});

// ---- episode-wanted / movie-wanted ----------------------------------------

describe("episode-wanted reducer", () => {
  it.each(["update", "delete"] as const)(
    "%s invalidates [Series, Wanted]",
    (event) => {
      if (event === "update") update("episode-wanted", []);
      else del("episode-wanted", []);

      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, QueryKeys.Wanted],
      });
    },
  );
});

describe("movie-wanted reducer", () => {
  it.each(["update", "delete"] as const)(
    "%s invalidates [Movies, Wanted]",
    (event) => {
      if (event === "update") update("movie-wanted", []);
      else del("movie-wanted", []);

      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: [QueryKeys.Movies, QueryKeys.Wanted],
      });
    },
  );
});

// ---- "any" key reducers ---------------------------------------------------

describe("any-event reducers", () => {
  it.each([
    ["settings", [QueryKeys.System]],
    ["languages", [QueryKeys.System, QueryKeys.Languages]],
    ["badges", [QueryKeys.System, QueryKeys.Badges]],
    ["backup", [QueryKeys.System, QueryKeys.Backups]],
    ["movie-history", [QueryKeys.Movies, QueryKeys.History]],
    ["movie-blacklist", [QueryKeys.Movies, QueryKeys.Blacklist]],
    [
      "episode-history",
      [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.History],
    ],
    [
      "episode-blacklist",
      [QueryKeys.Series, QueryKeys.Episodes, QueryKeys.Blacklist],
    ],
    ["reset-episode-wanted", [QueryKeys.Series, QueryKeys.Wanted]],
    ["reset-movie-wanted", [QueryKeys.Movies, QueryKeys.Wanted]],
    ["task", [QueryKeys.System, QueryKeys.Tasks]],
  ] as const)(
    '"%s" any event invalidates queryKey %j',
    (key, expectedQueryKey) => {
      any(key as string);

      expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
        queryKey: expectedQueryKey,
      });
    },
  );
});

// ---- jobs reducer (inline cache path) ------------------------------------

describe("jobs reducer (inline progress_value path)", () => {
  const jobsKey = [QueryKeys.System, QueryKeys.Jobs];

  it("inserts a new job into an empty cache when progress_value is present", () => {
    queryClientMock.getQueryData.mockReturnValue([]);

    update("jobs", [
      /* eslint-disable camelcase */
      {
        job_id: 1,
        progress_value: 0,
        progress_max: 100,
        progress_message: "Starting",
        status: "running",
      },
      /* eslint-enable camelcase */
    ]);

    expect(queryClientMock.setQueryData).toHaveBeenCalledWith(
      jobsKey,
      expect.arrayContaining([
        expect.objectContaining({
          // eslint-disable-next-line camelcase
          job_id: 1,
          // eslint-disable-next-line camelcase
          progress_value: 0,
          status: "running",
        }),
      ]),
    );
  });

  it("updates an existing job in the cache by job_id", () => {
    queryClientMock.getQueryData.mockReturnValue([
      /* eslint-disable camelcase */
      {
        job_id: 42,
        progress_value: 10,
        progress_max: 100,
        progress_message: "Old",
        status: "running",
      },
      /* eslint-enable camelcase */
    ]);

    update("jobs", [
      /* eslint-disable camelcase */
      {
        job_id: 42,
        progress_value: 50,
        progress_max: 100,
        progress_message: "Halfway",
        status: "running",
      },
      /* eslint-enable camelcase */
    ]);

    const calls = queryClientMock.setQueryData.mock.calls as [
      unknown,
      unknown[],
    ][];
    const [, result] = calls[0];

    const updated = (result as Record<string, unknown>[]).find(
      (j) => j.job_id === 42,
    );
    expect(updated).toBeDefined();

    expect(updated!.progress_value).toBe(50);

    expect(updated!.progress_message).toBe("Halfway");
  });

  it("trims the cache to 100 entries when it exceeds 100 jobs", () => {
    /* eslint-disable camelcase */
    const existing = Array.from({ length: 100 }, (_, i) => ({
      job_id: i,
      progress_value: 0,
      status: "done",
    }));
    /* eslint-enable camelcase */
    queryClientMock.getQueryData.mockReturnValue(existing);

    update("jobs", [
      /* eslint-disable camelcase */
      {
        job_id: 200,
        progress_value: 1,
        progress_max: 10,
        progress_message: "New",
        status: "running",
      },
      /* eslint-enable camelcase */
    ]);

    const calls2 = queryClientMock.setQueryData.mock.calls as [
      unknown,
      unknown[],
    ][];
    const [, result] = calls2[0];
    expect((result as unknown[]).length).toBe(100);
  });

  it("does not call setQueryData when progress_value is null (API fetch path)", () => {
    update("jobs", [
      /* eslint-disable camelcase */
      {
        job_id: 99,
        progress_value: null,
        progress_message: "Done",
        status: "finished",
      },
      /* eslint-enable camelcase */
    ]);

    expect(queryClientMock.setQueryData).not.toHaveBeenCalled();
  });
});

describe("sports reducer", () => {
  it("any event invalidates every cached sports query, wanted included", () => {
    any("sports");

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Sports],
    });
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Badges],
    });
  });
});
