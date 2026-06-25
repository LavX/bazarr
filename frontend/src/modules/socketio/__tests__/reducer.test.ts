/**
 * Behavior tests for createDefaultReducer().
 *
 * Covers every reducer key: verifies that socket events trigger the correct
 * queryClient.invalidateQueries calls (with exact query keys) and that the
 * "progress" reducer shows / updates / hides Mantine notifications correctly.
 *
 * The "episode" reducer (local-id cache lookup + series fallback) is already
 * exercised by the sibling reducer.test.ts; here we focus on the gaps:
 * progress notifications, inline jobs cache mutation, all "any" key reducers,
 * connect/disconnect lifecycle, and the "message" notification path.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";

// ---------------------------------------------------------------------------
// Hoist mocks so vi.mock factories can reference them
// ---------------------------------------------------------------------------

const queryClientMock = vi.hoisted(() => ({
  getQueryData: vi.fn(),
  invalidateQueries: vi.fn(),
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

// Use the REAL notification module so we can assert on the exact shape it
// produces (the progress reducer calls notification.progress.* directly).
vi.mock("@/modules/task", async (importOriginal) => {
  const real = await importOriginal<typeof import("@/modules/task")>();
  return { ...real };
});

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

// ---- message reducer ------------------------------------------------------

describe("message reducer", () => {
  it("calls showNotification once per incoming message", () => {
    update("message", ["Download complete", "Subtitle synced"]);
    expect(showNotificationMock).toHaveBeenCalledTimes(2);
  });

  it("shows a notification with the message text", () => {
    update("message", ["Hello"]);
    const [args] = showNotificationMock.mock.calls;
    expect(args[0]).toMatchObject({ message: "Hello" });
  });
});

// ---- progress reducer -----------------------------------------------------

describe("progress reducer", () => {
  const item = (
    overrides: Partial<{
      id: string;
      header: string;
      name: string;
      value: number;
      count: number;
    }> = {},
  ) => ({
    id: "prog-1",
    header: "Downloading",
    name: "sub.srt",
    value: 1,
    count: 5,
    ...overrides,
  });

  it("always calls showNotification (pending) before updateNotification", () => {
    update("progress", [item()]);

    expect(showNotificationMock).toHaveBeenCalledTimes(1);
    expect(updateNotificationMock).toHaveBeenCalledTimes(1);

    // showNotification must come before updateNotification
    const showOrder = showNotificationMock.mock.invocationCallOrder[0];
    const updateOrder = updateNotificationMock.mock.invocationCallOrder[0];
    expect(showOrder).toBeLessThan(updateOrder);
  });

  it("showNotification receives a pending notification with the correct id and header", () => {
    update("progress", [item({ id: "x1", header: "Syncing" })]);

    expect(showNotificationMock).toHaveBeenCalledWith(
      expect.objectContaining({ id: "x1", title: "Syncing", loading: true }),
    );
  });

  it("calls updateNotification with an in-progress payload when value < count", () => {
    update("progress", [item({ value: 2, count: 10 })]);

    expect(updateNotificationMock).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "prog-1",
        loading: true,
        autoClose: false,
        message: expect.stringContaining("2/10"),
      }),
    );
  });

  it("calls updateNotification with a completion payload when value === count", () => {
    update("progress", [item({ value: 5, count: 5, header: "Done" })]);

    expect(updateNotificationMock).toHaveBeenCalledWith(
      expect.objectContaining({
        id: "prog-1",
        message: "All Tasks Completed",
        color: "green",
      }),
    );
  });

  it("calls updateNotification with a completion payload when value > count", () => {
    update("progress", [item({ value: 6, count: 5 })]);

    expect(updateNotificationMock).toHaveBeenCalledWith(
      expect.objectContaining({ message: "All Tasks Completed" }),
    );
  });

  it("handles multiple items in one event, calling show+update for each", () => {
    update("progress", [
      item({ id: "a", value: 1, count: 3 }),
      item({ id: "b", value: 3, count: 3 }),
    ]);

    expect(showNotificationMock).toHaveBeenCalledTimes(2);
    expect(updateNotificationMock).toHaveBeenCalledTimes(2);
  });

  it("delete: calls hideNotification for each id (inside setTimeout)", () => {
    vi.useFakeTimers();

    del("progress", ["prog-1", "prog-2"]);

    // Not yet called — it's behind a setTimeout
    expect(hideNotificationMock).not.toHaveBeenCalled();

    vi.runAllTimers();

    expect(hideNotificationMock).toHaveBeenCalledWith("prog-1");
    expect(hideNotificationMock).toHaveBeenCalledWith("prog-2");

    vi.useRealTimers();
  });

  it("progress pending notification has loading: true and no autoClose", () => {
    update("progress", [item()]);
    const pendingCall = showNotificationMock.mock.calls[0][0] as Record<
      string,
      unknown
    >;
    expect(pendingCall.loading).toBe(true);
    // Pending notifications should not auto-close
    expect(pendingCall.autoClose).toBeUndefined();
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
