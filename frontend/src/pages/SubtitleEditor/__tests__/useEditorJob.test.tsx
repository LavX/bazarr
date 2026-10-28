/* eslint-disable camelcase */
import { PropsWithChildren } from "react";
import {
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { act, render, renderHook, waitFor } from "@testing-library/react";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { QueryKeys } from "@/apis/queries/keys";
import { useEditorJob } from "@/pages/SubtitleEditor/editorJobs";
import LineTranslationWatcher from "@/pages/SubtitleEditor/LineTranslationWatcher";
import server from "@/tests/mocks/node";

const JOBS_KEY = [QueryKeys.System, QueryKeys.Jobs];

let client: QueryClient;
// What the queue still answers for a job asked for by id.
let queue: System.Jobs[];

function wrapper({ children }: PropsWithChildren) {
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

function row(overrides: Partial<System.Jobs>): System.Jobs {
  return {
    job_id: 7,
    session: "run-a",
    job_name: "Translating",
    status: "running",
    last_run_time: "",
    is_progress: true,
    is_signalr: false,
    progress_value: 0,
    progress_max: 0,
    progress_message: "",
    ...overrides,
  };
}

beforeEach(() => {
  onlineManager.setOnline(true);
  client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity } },
  });
  queue = [];
  server.use(
    http.get("/api/system/jobs", ({ request }) => {
      const id = new URL(request.url).searchParams.get("id");
      return HttpResponse.json({
        data: queue.filter((job) => id === null || job.job_id === Number(id)),
      });
    }),
  );
});

describe("useEditorJob", () => {
  it("ignores a job of an earlier backend run that shares the id", async () => {
    client.setQueryData(JOBS_KEY, [
      row({ session: "run-before", status: "completed" }),
    ]);

    const { result } = renderHook(() => useEditorJob(7, "run-after"), {
      wrapper,
    });

    // The earlier run's finished row is not this job's end.
    expect(result.current).toBeUndefined();

    act(() => {
      client.setQueryData(JOBS_KEY, [
        row({ session: "run-before", status: "completed" }),
        row({ session: "run-after", status: "failed" }),
      ]);
    });
    await waitFor(() => expect(result.current?.status).toBe("failed"));
    expect(result.current?.session).toBe("run-after");
  });

  it("calls a job that was waiting and is gone from the queue cancelled, at once", async () => {
    queue = [row({ status: "pending" })];
    client.setQueryData(JOBS_KEY, [row({ status: "pending" })]);
    const { result } = renderHook(() => useEditorJob(7, "run-a"), { wrapper });
    await waitFor(() => expect(result.current?.status).toBe("pending"));

    // Cancelled from the Jobs drawer: removed from the queue and the cache.
    queue = [];
    act(() => {
      client.setQueryData(JOBS_KEY, []);
    });

    await waitFor(() => expect(result.current?.status).toBe("cancelled"));
  });

  it("keeps the old reading for a job it never saw: it ended and aged out", async () => {
    client.setQueryData(JOBS_KEY, []);
    queue = [];

    const { result } = renderHook(() => useEditorJob(9, "run-a"), { wrapper });

    await waitFor(() => expect(result.current?.status).toBe("completed"));
  });
});

describe("LineTranslationWatcher", () => {
  it("hands each line to the cue it was meant for, so two lines in flight are both kept", async () => {
    const lines: Array<[number, string]> = [];
    const done: number[] = [];
    server.use(
      http.get("/api/translator/editor", ({ request }) => {
        const jobId = Number(new URL(request.url).searchParams.get("jobId"));
        return HttpResponse.json({
          jobId,
          status: "completed",
          lines: [{ position: 0, line: `translated ${jobId}` }],
        });
      }),
    );
    client.setQueryData(JOBS_KEY, [
      row({ job_id: 7, status: "completed" }),
      row({ job_id: 8, status: "completed" }),
    ]);
    render(
      <>
        {[
          { jobId: 7, session: "run-a", targetIdx: 2 },
          { jobId: 8, session: "run-a", targetIdx: 5 },
        ].map((job) => (
          <LineTranslationWatcher
            key={job.jobId}
            job={job}
            onLine={(index, line) => lines.push([index, line])}
            onDone={(jobId) => done.push(jobId)}
          />
        ))}
      </>,
      { wrapper },
    );

    await waitFor(() => expect(done.sort()).toEqual([7, 8]));
    expect(lines.sort((a, b) => a[0] - b[0])).toEqual([
      [2, "translated 7"],
      [5, "translated 8"],
    ]);
  });
});
