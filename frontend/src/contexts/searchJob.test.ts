/* eslint-disable camelcase -- API fields keep their transport names. */
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "@/apis/raw";
import type { DiscoverSearchSnapshot } from "@/types/discover";
import { followSearch, SearchJobFailed, stopSearchJob } from "./searchJob";

vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      jobResult: vi.fn(),
      jobs: vi.fn(),
      deleteJobs: vi.fn(),
      actionOnJobs: vi.fn(),
    },
  },
}));

const jobResult = vi.mocked(api.system.jobResult);
const jobs = vi.mocked(api.system.jobs);
const deleteJobs = vi.mocked(api.system.deleteJobs);
const actionOnJobs = vi.mocked(api.system.actionOnJobs);
const snapshot = {
  search_id: "s",
  results: [],
} as unknown as DiscoverSearchSnapshot;

function outcome(
  status: "running" | "completed" | "failed" | "cancelled",
  extra: Partial<System.JobResult<unknown>> = {},
) {
  return {
    job_id: 5,
    session: "run-a",
    status,
    error: null,
    result: null,
    ...extra,
  } as System.JobResult<never>;
}

function follow(
  onProgress = vi.fn(),
  retired = () => false,
  session: string | undefined = "run-a",
) {
  const controller = new AbortController();
  return {
    controller,
    onProgress,
    done: followSearch(5, controller.signal, retired, onProgress, session),
  };
}

beforeEach(() => {
  jobResult.mockReset();
  vi.useFakeTimers();
});

describe("following a search job", () => {
  it("shows what the running search has so far and returns the snapshot it ends with", async () => {
    const live = { phase: "searching", providers: [], results: [] };
    jobResult
      .mockResolvedValueOnce(outcome("running", { result: live as never }))
      .mockResolvedValueOnce(
        outcome("running", {
          result: { phase: "finished", providers: [], snapshot } as never,
        }),
      )
      .mockResolvedValueOnce(
        outcome("completed", {
          result: { phase: "finished", providers: [], snapshot } as never,
        }),
      );
    const { done, onProgress } = follow();

    await vi.advanceTimersByTimeAsync(5000);

    await expect(done).resolves.toBe(snapshot);
    // The observation is shown; the look that already carries the snapshot is not.
    expect(onProgress).toHaveBeenCalledTimes(1);
    expect(onProgress).toHaveBeenCalledWith(live);
  });

  it("fails with the job's own sentence, but not with the queue's generic one", async () => {
    jobResult.mockResolvedValueOnce(
      outcome("failed", {
        error: { reason: "metadata_expired", message: "Reload details." },
      }),
    );
    const named = follow();
    const namedFailure = expect(named.done).rejects.toMatchObject({
      reason: "Reload details.",
    });
    await vi.advanceTimersByTimeAsync(1000);
    await namedFailure;

    jobResult.mockResolvedValueOnce(
      outcome("failed", {
        error: {
          reason: "unexpected_error",
          message: "The job failed unexpectedly.",
        },
      }),
    );
    const generic = follow();
    const genericFailure = expect(generic.done).rejects.toMatchObject({
      reason: undefined,
    });
    await vi.advanceTimersByTimeAsync(1000);
    await genericFailure;
  });

  it("says a stopped search was stopped, and gives up on a job that left the history", async () => {
    jobResult.mockResolvedValueOnce(outcome("cancelled"));
    const stopped = follow();
    const stoppedFailure = expect(stopped.done).rejects.toMatchObject({
      reason: "The search was stopped.",
    });
    await vi.advanceTimersByTimeAsync(1000);
    await stoppedFailure;

    jobResult.mockRejectedValueOnce({ response: { status: 404 } });
    const gone = follow();
    const goneFailure = expect(gone.done).rejects.toBeInstanceOf(
      SearchJobFailed,
    );
    await vi.advanceTimersByTimeAsync(1000);
    await goneFailure;
  });

  it("keeps asking after a look is lost, and stops once the reader has moved on", async () => {
    jobResult.mockRejectedValueOnce(new Error("offline")).mockResolvedValue(
      outcome("completed", {
        result: { phase: "finished", providers: [], snapshot } as never,
      }),
    );
    const kept = follow();
    await vi.advanceTimersByTimeAsync(2000);
    await expect(kept.done).resolves.toBe(snapshot);

    jobResult.mockReset();
    jobResult.mockResolvedValue(outcome("running"));
    let retired = false;
    const left = follow(vi.fn(), () => retired);
    const leftFailure = expect(left.done).rejects.toBeInstanceOf(
      SearchJobFailed,
    );
    await vi.advanceTimersByTimeAsync(1000);
    retired = true;
    await vi.advanceTimersByTimeAsync(1000);
    await leftFailure;
    const asked = jobResult.mock.calls.length;
    await vi.advanceTimersByTimeAsync(5000);
    expect(jobResult.mock.calls.length).toBe(asked);
  });

  it("does not take another backend run's job with the same id for its own", async () => {
    jobResult.mockResolvedValue(
      outcome("completed", {
        session: "run-b",
        result: { phase: "finished", providers: [], snapshot } as never,
      }),
    );
    const other = follow();
    const failure = expect(other.done).rejects.toMatchObject({
      reason: "The result is no longer available. Run it again.",
    });
    await vi.advanceTimersByTimeAsync(1000);
    await failure;
    // It looked once and stopped, instead of asking every 600 ms for ever.
    expect(jobResult).toHaveBeenCalledTimes(1);
  });

  it("stops asking once the job completed without a snapshot", async () => {
    jobResult.mockResolvedValue(
      outcome("completed", { result: { phase: "finished", providers: [] } }),
    );
    const bare = follow();
    const failure = expect(bare.done).rejects.toMatchObject({
      reason: "The result is no longer available. Run it again.",
    });
    await vi.advanceTimersByTimeAsync(1000);
    await failure;
    await vi.advanceTimersByTimeAsync(5000);
    expect(jobResult).toHaveBeenCalledTimes(1);
  });

  it("cannot follow a search that was never queued", async () => {
    const controller = new AbortController();
    await expect(
      followSearch(null, controller.signal, () => false, vi.fn()),
    ).rejects.toBeInstanceOf(SearchJobFailed);
  });
});

describe("stopping a search nobody reads", () => {
  function listed(status: string, session = "run-a") {
    jobs.mockResolvedValue([{ job_id: 5, status, session }] as never);
  }

  beforeEach(() => {
    jobs.mockReset();
    deleteJobs.mockReset();
    actionOnJobs.mockReset();
    vi.useRealTimers();
  });

  it("takes a search that is still waiting for a slot off the queue", async () => {
    listed("pending");
    await stopSearchJob(5, "run-a");
    expect(deleteJobs).toHaveBeenCalledWith(5);
    expect(actionOnJobs).not.toHaveBeenCalled();
  });

  it("asks a running search to stop", async () => {
    listed("running");
    await stopSearchJob(5, "run-a");
    expect(actionOnJobs).toHaveBeenCalledWith(5, "cancel");
    expect(deleteJobs).not.toHaveBeenCalled();
  });

  it("leaves a finished search and a job of another backend run alone", async () => {
    listed("completed");
    await stopSearchJob(5, "run-a");
    listed("running", "run-b");
    await stopSearchJob(5, "run-a");
    expect(deleteJobs).not.toHaveBeenCalled();
    expect(actionOnJobs).not.toHaveBeenCalled();
  });

  it("ignores a failure to ask", async () => {
    jobs.mockRejectedValue(new Error("offline"));
    await expect(stopSearchJob(5, "run-a")).resolves.toBeUndefined();
  });
});
