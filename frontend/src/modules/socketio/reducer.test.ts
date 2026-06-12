import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";

const queryClientMock = vi.hoisted(() => ({
  getQueryData: vi.fn(),
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

vi.mock("@/modules/task", () => ({
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
  beforeEach(() => {
    queryClientMock.getQueryData.mockReset();
    queryClientMock.invalidateQueries.mockReset();
    queryClientMock.setQueryData.mockReset();
  });

  it.each(["update", "delete"] as const)(
    "invalidates the local series query for episode %s events",
    (event) => {
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
      expect(queryClientMock.invalidateQueries).not.toHaveBeenCalledWith({
        queryKey: [QueryKeys.Series, 42],
      });
    },
  );

  it("falls back to the series list when an episode is not cached", () => {
    queryClientMock.getQueryData.mockReturnValue(undefined);

    emitEpisode("update", [9001]);

    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: [QueryKeys.Series],
    });
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
});
