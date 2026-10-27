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
