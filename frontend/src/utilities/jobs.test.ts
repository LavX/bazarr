import { QueryClient } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import {
  JOB_FALLBACK_POLL_MS,
  JobFailedError,
  UNKNOWN_JOB_OUTCOME,
  waitForJob,
} from "./jobs";

vi.mock("@/apis/raw", () => ({
  default: { system: { jobs: vi.fn() } },
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
      // eslint-disable-next-line camelcase
      job(1, "failed", { progress_message: "Could not install X: offline" }),
    ]);

    await expect(waiting).rejects.toBeInstanceOf(JobFailedError);
    await expect(waitForJob(client, 1)).rejects.toThrow(
      "Could not install X: offline",
    );
  });

  it("prefers the job's error field when the backend sends one", async () => {
    client.setQueryData(JOBS_KEY, [
      {
        ...job(2, "failed", {
          // eslint-disable-next-line camelcase
          progress_message: "Installing",
        }),
        error: "hash mismatch",
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
      // eslint-disable-next-line camelcase
      job(4, "failed", { progress_message: "Refresh failed" }),
    ]);
    const waiting = waitForJob(client, 4);
    const outcome = expect(waiting).rejects.toThrow("Refresh failed");

    await vi.advanceTimersByTimeAsync(JOB_FALLBACK_POLL_MS);
    await outcome;
    // One request for everything waiting, not one per job.
    expect(mockedJobs).toHaveBeenCalledTimes(1);
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
