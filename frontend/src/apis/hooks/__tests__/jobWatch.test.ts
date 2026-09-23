import { QueryClient } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { refreshWhenJobFinishes, whenJobFinishes } from "@/apis/hooks/jobWatch";
import { QueryKeys } from "@/apis/queries/keys";

const JOBS = [QueryKeys.System, QueryKeys.Jobs];

// The socket reducer writes each jobs event into this cache; the tests write
// the same shape it does.
function setJob(client: QueryClient, jobId: number, status: string) {
  client.setQueryData(JOBS, [
    { job_id: 1, status: "running" },
    { job_id: jobId, status },
  ]);
}

describe("whenJobFinishes", () => {
  it("waits for the job's terminal event and fires once", () => {
    const client = new QueryClient();
    const done = vi.fn();
    whenJobFinishes(client, 7, done);

    setJob(client, 7, "pending");
    setJob(client, 7, "running");
    expect(done).not.toHaveBeenCalled();

    setJob(client, 7, "completed");
    expect(done).toHaveBeenCalledExactlyOnceWith("completed");

    setJob(client, 7, "completed");
    expect(done).toHaveBeenCalledTimes(1);
  });

  it("treats a failed job as finished", () => {
    const client = new QueryClient();
    const done = vi.fn();
    whenJobFinishes(client, 7, done);
    setJob(client, 7, "failed");
    expect(done).toHaveBeenCalledWith("failed");
  });

  it("fires at once for a job that finished before the watch started", () => {
    const client = new QueryClient();
    setJob(client, 7, "completed");
    const done = vi.fn();
    whenJobFinishes(client, 7, done);
    expect(done).toHaveBeenCalledWith("completed");
  });

  it("ignores other jobs and other queries", () => {
    const client = new QueryClient();
    const done = vi.fn();
    whenJobFinishes(client, 7, done);
    setJob(client, 8, "completed");
    client.setQueryData([QueryKeys.Series], [{ job_id: 7, status: "failed" }]);
    expect(done).not.toHaveBeenCalled();
  });

  it("stops watching when asked", () => {
    const client = new QueryClient();
    const done = vi.fn();
    const stop = whenJobFinishes(client, 7, done);
    stop();
    setJob(client, 7, "completed");
    expect(done).not.toHaveBeenCalled();
  });
});

describe("refreshWhenJobFinishes", () => {
  it("refreshes at once when nothing was queued", () => {
    const client = new QueryClient();
    const refresh = vi.fn();
    refreshWhenJobFinishes(client, null, refresh);
    expect(refresh).toHaveBeenCalledTimes(1);
  });

  it("waits for the queued job", () => {
    const client = new QueryClient();
    const refresh = vi.fn();
    refreshWhenJobFinishes(client, 7, refresh);
    expect(refresh).not.toHaveBeenCalled();
    setJob(client, 7, "completed");
    expect(refresh).toHaveBeenCalledTimes(1);
  });
});
