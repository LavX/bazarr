import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import {
  awaitJobResult,
  JOB_FALLBACK_POLL_MS,
  JOB_RESULT_GONE,
  JobFailedError,
  JobOutcomeUnknownError,
  JobStoppedError,
  UNKNOWN_JOB_OUTCOME,
  waitForJob,
  waitForJobs,
} from "./jobs";

vi.mock("@/apis/raw", () => ({
  default: { system: { jobs: vi.fn(), jobResult: vi.fn() } },
}));

const mockedJobs = vi.mocked(api.system.jobs);
const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];

function job(id: number, status: string, extra: Partial<System.Jobs> = {}) {
  return {
    // eslint-disable-next-line camelcase
    job_id: id,
    // eslint-disable-next-line camelcase
    job_name: `Job ${id}`,
    status,
    // eslint-disable-next-line camelcase
    progress_message: "",
    ...extra,
  } as System.Jobs;
}

describe("waitForJob", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient();
    mockedJobs.mockReset();
    // By default the fallback poll finds the job still running.
    mockedJobs.mockImplementation(async () => [job(1, "running")]);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("resolves once the jobs cache reports the job completed", async () => {
    client.setQueryData(JOBS_KEY, [job(1, "pending")]);
    let settled = false;
    const waiting = waitForJob(client, 1).then(() => {
      settled = true;
    });

    await Promise.resolve();
    expect(settled).toBe(false);

    client.setQueryData(JOBS_KEY, [job(1, "completed")]);
    await waiting;
    expect(settled).toBe(true);
  });

  it("rejects with the reason the job failed with", async () => {
    const waiting = waitForJob(client, 1);
    client.setQueryData(JOBS_KEY, [
      job(1, "failed", {
        error: { reason: "failed", message: "Could not install X: offline" },
      }),
    ]);

    await expect(waiting).rejects.toBeInstanceOf(JobFailedError);
    await expect(waitForJob(client, 1)).rejects.toThrow(
      "Could not install X: offline",
    );
  });

  it("rejects a job that was stopped instead of resolving it", async () => {
    // The queue records a stopped job as completed, so only the flag says it
    // did not finish. Resolved, the wizard counted a stopped install as staged.
    const waiting = waitForJob(client, 6);
    client.setQueryData(JOBS_KEY, [
      job(6, "completed", {
        stopped: true,
        // eslint-disable-next-line camelcase
        progress_message: "Cancelled by user",
      }),
    ]);

    await expect(waiting).rejects.toBeInstanceOf(JobStoppedError);
    await expect(waitForJob(client, 6)).rejects.toThrow("Job 6 was stopped");
  });

  it("rejects a job the queue reports cancelled, flag or no flag", async () => {
    // The backend names the stop in the status itself; the flag is kept for
    // rows recorded while a stopped job still read as completed.
    client.setQueryData(JOBS_KEY, [
      job(7, "cancelled", {
        // eslint-disable-next-line camelcase
        progress_message: "Cancelled by user",
      }),
    ]);

    await expect(waitForJob(client, 7)).rejects.toBeInstanceOf(JobStoppedError);
    await expect(waitForJob(client, 7)).rejects.toThrow("Job 7 was stopped");
  });

  it("reads the reason from the job's error, not its progress message", async () => {
    client.setQueryData(JOBS_KEY, [
      {
        ...job(2, "failed", {
          // eslint-disable-next-line camelcase
          progress_message: "Installing",
        }),
        error: { reason: "failed", message: "hash mismatch" },
      },
    ]);

    await expect(waitForJob(client, 2)).rejects.toThrow("hash mismatch");
  });

  it("resolves at once for a job that finished before the wait began", async () => {
    client.setQueryData(JOBS_KEY, [job(3, "completed")]);
    await expect(waitForJob(client, 3)).resolves.toMatchObject({
      // eslint-disable-next-line camelcase
      job_id: 3,
    });
  });

  it("resolves at once when there is no job to follow", async () => {
    await expect(waitForJob(client, null)).resolves.toBeUndefined();
  });

  it("falls back to reading the job list when the socket stays silent", async () => {
    vi.useFakeTimers();
    mockedJobs.mockImplementation(async () => [
      job(4, "failed", {
        error: { reason: "failed", message: "Refresh failed" },
      }),
    ]);
    const waiting = waitForJob(client, 4);
    const outcome = expect(waiting).rejects.toThrow("Refresh failed");

    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    await outcome;
    // One request for everything waiting, not one per job.
    expect(mockedJobs).toHaveBeenCalledTimes(1);
  });

  it("does not settle a reused id on the row a restarted backend left behind", async () => {
    // Job ids restart at 1 with the backend, so the finished job 1 of the
    // previous run is still cached when the socket connects again.
    const session = createDefaultReducer().find(
      (reducer) => reducer.key === "session",
    )?.update as (sessions: string[]) => void;
    session(["run-before"]);
    queryClient.setQueryData(JOBS_KEY, [job(1, "completed")]);
    // The socket connects again and the backend names a new run.
    session(["run-after"]);

    let settled = false;
    const waiting = waitForJob(queryClient, 1).then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    // The new run's job 1 finishing is what settles it.
    queryClient.setQueryData(JOBS_KEY, [job(1, "completed")]);
    await waiting;
    expect(settled).toBe(true);
  });

  it("waits for the job of its own backend run, not a row of another run that shares the id", async () => {
    client.setQueryData(JOBS_KEY, [
      job(1, "completed", { session: "run-before" }),
    ]);
    let settled = false;
    const waiting = waitForJob(client, 1, "run-after").then(() => {
      settled = true;
    });
    await Promise.resolve();
    expect(settled).toBe(false);

    client.setQueryData(JOBS_KEY, [
      job(1, "completed", { session: "run-before" }),
      job(1, "completed", { session: "run-after" }),
    ]);
    await waiting;
    expect(settled).toBe(true);
  });

  it("tells a job it can no longer follow from a job that failed", async () => {
    vi.useFakeTimers();
    mockedJobs.mockImplementation(async () => []);
    const waiting = waitForJob(client, 5);
    const unknown = expect(waiting).rejects.toBeInstanceOf(
      JobOutcomeUnknownError,
    );

    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    await unknown;
    // It is still a failure to follow, so callers that only know failures keep working.
    await expect(waiting).rejects.toBeInstanceOf(JobFailedError);

    const failed = waitForJob(client, 6);
    client.setQueryData(JOBS_KEY, [
      job(6, "failed", { error: { reason: "x", message: "Real failure" } }),
    ]);
    await expect(failed).rejects.not.toBeInstanceOf(JobOutcomeUnknownError);
  });

  it("does not report a job the backend no longer lists as a success", async () => {
    vi.useFakeTimers();
    mockedJobs.mockImplementation(async () => []);
    const waiting = waitForJob(client, 5);
    const outcome = expect(waiting).rejects.toThrow(UNKNOWN_JOB_OUTCOME);

    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    await outcome;
  });
});

describe("waitForJobs", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient();
    mockedJobs.mockReset();
    mockedJobs.mockImplementation(async () => []);
  });

  const scans = (spy: ReturnType<typeof vi.spyOn>) =>
    spy.mock.calls.filter(
      ([key]: unknown[]) => JSON.stringify(key) === JSON.stringify(JOBS_KEY),
    ).length;

  it("reads the jobs cache once for a whole batch of children, not once per child", async () => {
    const count = 3000;
    const ids = Array.from({ length: count }, (_, index) => index + 1);
    client.setQueryData(
      JOBS_KEY,
      ids.map((id) => job(id, "pending")),
    );
    const spy = vi.spyOn(client, "getQueryData");

    const startedAt = Date.now();
    const waiting = waitForJobs(client, ids);
    // One scan to see which already ended, whatever the number of children.
    expect(scans(spy)).toBe(1);
    // Generous: the per-child scan took 25 s at this size.
    expect(Date.now() - startedAt).toBeLessThan(5000);

    spy.mockClear();
    client.setQueryData(
      JOBS_KEY,
      ids.map((id) => job(id, "completed")),
    );
    const results = await waiting;
    expect(scans(spy)).toBeLessThanOrEqual(2);
    expect(results).toHaveLength(count);
    expect(results.every((entry) => entry.status === "fulfilled")).toBe(true);
  });

  it("settles each child on its own outcome and waits for the one still running", async () => {
    client.setQueryData(JOBS_KEY, [
      job(1, "failed", { error: { reason: "failed", message: "boom" } }),
      job(2, "running"),
      job(3, "cancelled"),
    ]);
    let done = false;
    const waiting = waitForJobs(client, [1, 2, 3]).then((results) => {
      done = true;
      return results;
    });

    await Promise.resolve();
    await Promise.resolve();
    expect(done).toBe(false);

    client.setQueryData(JOBS_KEY, [
      job(1, "failed", { error: { reason: "failed", message: "boom" } }),
      job(2, "completed"),
      job(3, "cancelled"),
    ]);
    const results = await waiting;
    expect(results.map((entry) => entry.status)).toEqual([
      "rejected",
      "fulfilled",
      "rejected",
    ]);
    expect((results[0] as PromiseRejectedResult).reason).toBeInstanceOf(
      JobFailedError,
    );
    expect((results[2] as PromiseRejectedResult).reason).toBeInstanceOf(
      JobStoppedError,
    );
  });

  it("keeps the session check: a row of another backend run is not the child", async () => {
    client.setQueryData(JOBS_KEY, [job(1, "completed", { session: "old" })]);
    let done = false;
    const waiting = waitForJobs(client, [1], "new").then((results) => {
      done = true;
      return results;
    });
    await Promise.resolve();
    await Promise.resolve();
    expect(done).toBe(false);

    client.setQueryData(JOBS_KEY, [job(1, "completed", { session: "new" })]);
    expect((await waiting)[0].status).toBe("fulfilled");
  });

  it("reports a child the backend stopped listing as unknown, not as done", async () => {
    vi.useFakeTimers();
    try {
      const waiting = waitForJobs(client, [7, null]);
      await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
      const results = await waiting;
      expect((results[0] as PromiseRejectedResult).reason).toBeInstanceOf(
        JobOutcomeUnknownError,
      );
      // An id that could not be queued resolves at once.
      expect(results[1].status).toBe("fulfilled");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("a pass that settles many children", () => {
  let client: QueryClient;

  beforeEach(() => {
    client = new QueryClient();
    mockedJobs.mockReset();
    mockedJobs.mockImplementation(async () => []);
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  const count = 10000;
  const ids = Array.from({ length: count }, (_, index) => index + 1);

  // Counts the subscriptions the module takes and gives back, so a leak or a
  // double release shows.
  function watchSubscriptions(target: QueryClient) {
    const cache = target.getQueryCache();
    const real = cache.subscribe.bind(cache);
    const released = vi.fn();
    const taken = vi.fn();
    vi.spyOn(cache, "subscribe").mockImplementation((listener) => {
      taken();
      const unsubscribe = real(listener);
      return () => {
        released();
        unsubscribe();
      };
    });
    return { taken, released };
  }

  // The fallback poll is the one interval here; the query cache's own timers
  // are not counted.
  function watchIntervals() {
    const live = new Set<unknown>();
    const set = globalThis.setInterval;
    const clear = globalThis.clearInterval;
    vi.spyOn(globalThis, "setInterval").mockImplementation(((
      ...args: Parameters<typeof setInterval>
    ) => {
      const id = set(...args);
      live.add(id);
      return id;
    }) as typeof setInterval);
    vi.spyOn(globalThis, "clearInterval").mockImplementation(((
      id: Parameters<typeof clearInterval>[0],
    ) => {
      live.delete(id);
      clear(id);
    }) as typeof clearInterval);
    return live;
  }

  // The release after a settle looked over the map of waiters once per settled
  // job. Map iteration is the unit of that work: the pass itself iterates the
  // map a fixed number of times.
  function countMapScans() {
    const values = vi.spyOn(Map.prototype, "values");
    const entries = vi.spyOn(Map.prototype, "entries");
    return () => values.mock.calls.length + entries.mock.calls.length;
  }

  it("settles children the backend stopped listing without a pass over the waiters per child", async () => {
    const intervals = watchIntervals();
    const subscriptions = watchSubscriptions(client);
    const waiting = waitForJobs(client, ids, "run-a");
    const scans = countMapScans();
    const startedAt = Date.now();

    // A backend restart mid-batch: the poll finds none of them.
    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    const results = await waiting;

    expect(results).toHaveLength(count);
    expect(
      results.every(
        (entry) =>
          entry.status === "rejected" &&
          entry.reason instanceof JobOutcomeUnknownError,
      ),
    ).toBe(true);
    expect(scans()).toBeLessThan(100);
    // Generous: the pass per child took seconds at this size.
    expect(Date.now() - startedAt).toBeLessThan(JOB_FALLBACK_POLL_MS + 1500);
    // Nothing is left behind once the last waiter settled.
    expect(subscriptions.taken).toHaveBeenCalledTimes(1);
    expect(subscriptions.released).toHaveBeenCalledTimes(1);
    expect(intervals.size).toBe(0);
  });

  it("settles children a queue clear took out of the list the same way", async () => {
    const intervals = watchIntervals();
    client.setQueryData(
      JOBS_KEY,
      ids.map((id) => job(id, "pending")),
    );
    const subscriptions = watchSubscriptions(client);
    const waiting = waitForJobs(client, ids, "run-a");
    const scans = countMapScans();

    // The drawer clears the pending queue without a per-job event, and the
    // list that comes back no longer has them.
    client.setQueryData(JOBS_KEY, []);
    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    const results = await waiting;

    expect(results.every((entry) => entry.status === "rejected")).toBe(true);
    expect(scans()).toBeLessThan(100);
    expect(subscriptions.released).toHaveBeenCalledTimes(1);
    expect(intervals.size).toBe(0);
  });

  it("settles in the order the jobs were queued", async () => {
    const order: number[] = [];
    const some = ids.slice(0, 50);
    const waiting = Promise.allSettled(
      some.map((id) =>
        waitForJob(client, id).catch(() => {
          order.push(id);
        }),
      ),
    );
    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    await waiting;
    expect(order).toEqual(some);
  });

  it("keeps listening while some children are still waiting, and lets go after the last", async () => {
    const intervals = watchIntervals();
    const subscriptions = watchSubscriptions(client);
    client.setQueryData(JOBS_KEY, [job(1, "pending"), job(2, "pending")]);
    const first = waitForJob(client, 1);
    const second = waitForJob(client, 2);

    client.setQueryData(JOBS_KEY, [job(1, "completed"), job(2, "running")]);
    await first;
    expect(subscriptions.released).not.toHaveBeenCalled();
    expect(intervals.size).toBe(1);

    client.setQueryData(JOBS_KEY, [job(1, "completed"), job(2, "completed")]);
    await second;
    expect(subscriptions.taken).toHaveBeenCalledTimes(1);
    expect(subscriptions.released).toHaveBeenCalledTimes(1);
    expect(intervals.size).toBe(0);
  });

  it("listens and polls again for a job followed after the last wait ended", async () => {
    const intervals = watchIntervals();
    const subscriptions = watchSubscriptions(client);
    const earlier = waitForJob(client, 1);
    client.setQueryData(JOBS_KEY, [job(1, "completed")]);
    await earlier;
    expect(intervals.size).toBe(0);

    const later = waitForJob(client, 2);
    expect(subscriptions.taken).toHaveBeenCalledTimes(2);
    expect(intervals.size).toBe(1);
    client.setQueryData(JOBS_KEY, [job(2, "completed")]);
    await expect(later).resolves.toMatchObject({
      // eslint-disable-next-line camelcase
      job_id: 2,
    });
    expect(subscriptions.released).toHaveBeenCalledTimes(2);
    expect(intervals.size).toBe(0);
  });

  it("lets go of a client nothing waits on while another still does", async () => {
    const intervals = watchIntervals();
    const other = new QueryClient();
    const mine = watchSubscriptions(client);
    const theirs = watchSubscriptions(other);
    const first = waitForJob(client, 1);
    const second = waitForJob(other, 2);

    client.setQueryData(JOBS_KEY, [job(1, "completed")]);
    await first;
    expect(mine.released).toHaveBeenCalledTimes(1);
    expect(theirs.released).not.toHaveBeenCalled();
    expect(intervals.size).toBe(1);

    other.setQueryData(JOBS_KEY, [job(2, "completed")]);
    await second;
    expect(theirs.released).toHaveBeenCalledTimes(1);
    expect(intervals.size).toBe(0);
  });

  it("polls a long list for many children without a scan of it per child", async () => {
    const list = ids.map((id) => job(id, "pending"));
    mockedJobs.mockImplementation(async () => list);
    const waiting = waitForJobs(client, ids, "run-a");
    const find = vi.spyOn(Array.prototype, "find");

    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    // Looking each child up in the list is a scan per child. The lookups left
    // are over the one or two rows that share an id.
    const longScans = find.mock.contexts.filter(
      (context) => (context as unknown[]).length >= count,
    ).length;
    expect(longScans).toBe(0);

    mockedJobs.mockImplementation(async () =>
      ids.map((id) => job(id, "completed")),
    );
    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    expect((await waiting).every((entry) => entry.status === "fulfilled")).toBe(
      true,
    );
  });

  it("reads the jobs cache only when the jobs list changes", async () => {
    client.setQueryData(JOBS_KEY, [job(1, "pending")]);
    const waiting = waitForJob(client, 1);
    const spy = vi.spyOn(client, "getQueryData");

    client.setQueryData([QueryKeys.System, "unrelated"], { busy: true });
    client.setQueryData([QueryKeys.Series], []);
    client.getQueryCache().find({ queryKey: [QueryKeys.Series] });
    expect(spy).not.toHaveBeenCalled();

    client.setQueryData(JOBS_KEY, [job(1, "completed")]);
    await waiting;
    expect(spy).toHaveBeenCalled();
  });
});

describe("awaitJobResult", () => {
  let client: QueryClient;
  const mockedResult = vi.mocked(api.system.jobResult);

  beforeEach(() => {
    client = new QueryClient();
    mockedResult.mockReset();
  });

  const outcome = (result: unknown) =>
    ({
      // eslint-disable-next-line camelcase
      job_id: 3,
      session: "run-a",
      status: "completed",
      error: null,
      result,
    }) as System.JobResult;

  it("hands back what the job published once it finished", async () => {
    client.setQueryData(JOBS_KEY, [job(3, "completed")]);
    mockedResult.mockResolvedValue(outcome([{ provider: "a" }]));

    await expect(awaitJobResult(client, 3)).resolves.toEqual([
      { provider: "a" },
    ]);
    expect(mockedResult).toHaveBeenCalledWith(3);
  });

  it("rejects with the job's reason when it failed, without asking for a result", async () => {
    client.setQueryData(JOBS_KEY, [
      job(3, "failed", { error: { reason: "x", message: "No provider." } }),
    ]);

    await expect(awaitJobResult(client, 3)).rejects.toThrow("No provider.");
    expect(mockedResult).not.toHaveBeenCalled();
  });

  it("says so when the job finished without publishing, or left the history", async () => {
    client.setQueryData(JOBS_KEY, [job(3, "completed")]);
    mockedResult.mockResolvedValueOnce(outcome(null));
    await expect(awaitJobResult(client, 3)).rejects.toThrow(JOB_RESULT_GONE);

    mockedResult.mockRejectedValueOnce({ response: { status: 404 } });
    await expect(awaitJobResult(client, 3)).rejects.toThrow(JOB_RESULT_GONE);
  });

  it("does not hand back the result of another backend run's job with the same id", async () => {
    client.setQueryData(JOBS_KEY, [job(3, "completed", { session: "run-a" })]);
    mockedResult.mockResolvedValue({
      ...outcome([{ provider: "other" }]),
      session: "run-b",
    });

    await expect(awaitJobResult(client, 3, "run-a")).rejects.toThrow(
      JOB_RESULT_GONE,
    );
  });

  it("refuses a search that could not be queued", async () => {
    await expect(awaitJobResult(client, null)).rejects.toThrow(
      "could not be queued",
    );
  });
});
