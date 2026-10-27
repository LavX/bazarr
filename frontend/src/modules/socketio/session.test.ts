/* eslint-disable camelcase -- socket payloads keep their transport field names. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";

const queryClientMock = vi.hoisted(() => ({
  getQueryData: vi.fn(),
  getQueryState: vi.fn(),
  cancelQueries: vi.fn(),
  invalidateQueries: vi.fn(),
  resetQueries: vi.fn(),
  setQueryData: vi.fn(),
}));

vi.mock("@/apis/queries", () => ({ default: queryClientMock }));
vi.mock("@/apis/raw", () => ({ default: { system: { jobs: vi.fn() } } }));
vi.mock("@/modules/notification", () => ({ notification: { info: vi.fn() } }));
vi.mock("@mantine/notifications", () => ({
  showNotification: vi.fn(),
  hideNotification: vi.fn(),
}));
vi.mock("@/utilities/console", () => ({
  LOG: vi.fn(),
  ENSURE: vi.fn(),
  GROUP: vi.fn(),
}));
vi.mock("@/utilities/event", () => ({ setOnlineStatus: vi.fn() }));

// The socket the client talks through: handlers it registered, and the
// session request it makes after each connect.
const socket = vi.hoisted(() => {
  const handlers = new Map<string, (...args: unknown[]) => void>();
  const sessionCallbacks: Array<
    (error: Error | null, reply?: unknown) => void
  > = [];
  return {
    handlers,
    sessionCallbacks,
    on: vi.fn((event: string, handler: (...args: unknown[]) => void) => {
      handlers.set(event, handler);
    }),
    timeout: vi.fn(() => ({
      emit: vi.fn(
        (
          _event: string,
          callback: (error: Error | null, reply?: unknown) => void,
        ) => {
          sessionCallbacks.push(callback);
        },
      ),
    })),
    connect: vi.fn(),
    disconnect: vi.fn(),
  };
});

vi.mock("socket.io-client", () => ({ io: vi.fn(() => socket) }));

import { claimJobCompletion, isJobCompletionClaimed } from "@/modules/jobs";
import { createDefaultReducer } from "./reducer";
import client from ".";

const JOBS = [QueryKeys.System, QueryKeys.Jobs];

// The client batches events behind a 3 ms debounce.
const settle = () => new Promise((resolve) => setTimeout(resolve, 20));

async function reconnect(reply: unknown, error: Error | null = null) {
  socket.handlers.get("connect")!();
  const callback = socket.sessionCallbacks.pop()!;
  callback(error, reply);
  await settle();
}

describe("socket reconnects and the backend session", () => {
  beforeEach(() => {
    queryClientMock.invalidateQueries.mockReset();
    queryClientMock.resetQueries.mockReset();
    queryClientMock.getQueryData.mockReset();
    queryClientMock.setQueryData.mockReset();
  });

  it("asks the backend which run it reached after every connect", async () => {
    createDefaultReducer()
      .filter(({ key }) => key === "connect" || key === "session")
      .forEach((reducer) => client.addReducer(reducer));
    await reconnect({ session: "run-one" });
    expect(socket.timeout).toHaveBeenCalled();
    // The first answer has nothing to compare with: the list is read again,
    // not dropped.
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: JOBS,
      exact: true,
    });
    expect(queryClientMock.resetQueries).not.toHaveBeenCalled();
  });

  it("keeps a claim across a reconnect to the same backend run", async () => {
    await reconnect({ session: "run-one" });
    expect(claimJobCompletion(41, "run-one")).toBe(true);

    queryClientMock.invalidateQueries.mockReset();
    await reconnect({ session: "run-one" });

    expect(isJobCompletionClaimed(41, "run-one")).toBe(true);
    expect(queryClientMock.resetQueries).not.toHaveBeenCalled();
    // What the gap missed is read again.
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: JOBS,
      exact: true,
    });
  });

  it("drops the claims and the jobs list when the backend restarted", async () => {
    await reconnect({ session: "run-one" });
    claimJobCompletion(42, "run-one");

    await reconnect({ session: "run-two" });

    expect(isJobCompletionClaimed(42, "run-one")).toBe(false);
    expect(queryClientMock.resetQueries).toHaveBeenCalledWith({
      queryKey: JOBS,
      exact: true,
    });
    // The new run's job that reuses the id is not the one claimed before.
    expect(claimJobCompletion(42, "run-two")).toBe(true);
  });

  it("still reads the jobs list again when the backend does not answer", async () => {
    await reconnect({ session: "run-two" });
    claimJobCompletion(43, "run-two");
    queryClientMock.invalidateQueries.mockReset();

    await reconnect(undefined, new Error("timeout"));

    expect(isJobCompletionClaimed(43, "run-two")).toBe(true);
    expect(queryClientMock.resetQueries).not.toHaveBeenCalled();
    expect(queryClientMock.invalidateQueries).toHaveBeenCalledWith({
      queryKey: JOBS,
      exact: true,
    });
  });
});

describe("jobs events and the backend session", () => {
  const jobsReducer = () =>
    createDefaultReducer().find(({ key }) => key === "jobs")!.update as (
      payload: unknown[],
    ) => void;

  beforeEach(() => {
    queryClientMock.resetQueries.mockReset();
    queryClientMock.setQueryData.mockReset();
    queryClientMock.getQueryData.mockReset();
  });

  it("drops the list when an event comes from a run the page has not seen", () => {
    // The handshake can be missed, and the events say which run they are from.
    jobsReducer()([
      { job_id: 1, session: "run-seen", progress_value: 1, status: "running" },
    ]);
    queryClientMock.resetQueries.mockReset();
    jobsReducer()([
      { job_id: 1, session: "run-seen", progress_value: 2, status: "running" },
    ]);
    expect(queryClientMock.resetQueries).not.toHaveBeenCalled();

    jobsReducer()([
      { job_id: 1, session: "run-new", progress_value: 1, status: "running" },
    ]);
    expect(queryClientMock.resetQueries).toHaveBeenCalledWith({
      queryKey: JOBS,
      exact: true,
    });
  });

  it("never merges an event into a cached row from another run that shares its id", () => {
    queryClientMock.getQueryData.mockReturnValue([
      {
        job_id: 9,
        session: "run-old",
        status: "completed",
        progress_message: "old",
      },
    ]);
    jobsReducer()([
      {
        job_id: 9,
        session: "run-newer",
        progress_value: 0,
        progress_max: 3,
        progress_message: "new",
        status: "running",
      },
    ]);
    const written = queryClientMock.setQueryData.mock.calls[0][1] as Array<{
      session: string;
      progress_message: string;
    }>;
    expect(written).toHaveLength(2);
    expect(written[0]).toMatchObject({
      session: "run-old",
      progress_message: "old",
    });
    expect(written[1]).toMatchObject({
      session: "run-newer",
      progress_message: "new",
    });
  });
});

describe("the jobs cache", () => {
  const jobsReducer = () =>
    createDefaultReducer().find(({ key }) => key === "jobs")!;

  beforeEach(() => {
    queryClientMock.setQueryData.mockReset();
    queryClientMock.getQueryData.mockReset();
  });

  it("drops a queued job the backend removed", () => {
    queryClientMock.getQueryData.mockReturnValue([
      { job_id: 3, session: "run-a", status: "pending" },
      { job_id: 4, session: "run-a", status: "pending" },
    ]);

    jobsReducer().delete?.([{ job_id: 3, session: "run-a" }] as never);

    expect(queryClientMock.setQueryData).toHaveBeenCalledWith(JOBS, [
      { job_id: 4, session: "run-a", status: "pending" },
    ]);
  });

  it("does not drop the job of another backend run that shares the id", () => {
    queryClientMock.getQueryData.mockReturnValue([
      { job_id: 3, session: "run-a", status: "completed" },
    ]);

    jobsReducer().delete?.([{ job_id: 3, session: "run-b" }] as never);

    expect(queryClientMock.setQueryData).toHaveBeenCalledWith(JOBS, [
      { job_id: 3, session: "run-a", status: "completed" },
    ]);
  });

  it("keeps the history of both outcomes, and trims ended jobs before active ones", async () => {
    const { MAX_JOBS_IN_CACHE, trimJobs } = await import("./reducer");
    const ended = Array.from({ length: 100 }, (_, index) => ({
      job_id: index,
      status: index % 2 ? "failed" : "completed",
    }));
    // Fifty of each outcome, which the backend now remembers, fit with room to spare.
    expect(MAX_JOBS_IN_CACHE).toBeGreaterThan(ended.length + 100);
    expect(trimJobs(ended)).toBe(ended);

    const crowded = [
      { job_id: -1, status: "running" },
      ...Array.from({ length: MAX_JOBS_IN_CACHE + 5 }, (_, index) => ({
        job_id: index,
        status: "completed",
      })),
      { job_id: 9999, status: "pending" },
    ];
    const trimmed = trimJobs(crowded);
    expect(trimmed).toHaveLength(MAX_JOBS_IN_CACHE);
    // The oldest ended jobs go; the one that is running and the one that is
    // pending stay, wherever they sit in the list.
    expect(trimmed.map((job) => job.job_id)).toContain(-1);
    expect(trimmed.map((job) => job.job_id)).toContain(9999);
    expect(trimmed.map((job) => job.job_id)).not.toContain(0);
    expect(trimmed.map((job) => job.job_id)).toContain(MAX_JOBS_IN_CACHE + 4);

    // Nothing but active jobs: none is ever dropped.
    const active = Array.from(
      { length: MAX_JOBS_IN_CACHE + 3 },
      (_, index) => ({
        job_id: index,
        status: "running",
      }),
    );
    expect(trimJobs(active)).toHaveLength(MAX_JOBS_IN_CACHE + 3);
  });
});

describe("the legacy socket channels", () => {
  it("no longer register a message or progress reducer", () => {
    const keys = createDefaultReducer().map(({ key }) => key as string);
    expect(keys).not.toContain("message");
    expect(keys).not.toContain("progress");
  });
});
