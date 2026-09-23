/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { http, HttpResponse } from "msw";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { act, screen, waitFor, within } from "@/tests";

/**
 * A Discover download is a standard job. These handlers stand in for the
 * backend side of that: the enqueue answers with a job id, the job's state is
 * served by /system/jobs, the finish arrives as a real jobs socket event
 * through the app's own reducer, and the file is fetched by its ticket.
 */
export interface DownloadRequest {
  result: string | null;
  search: string | null;
  authenticated: boolean;
}

export interface JobOutcome {
  status: "completed" | "failed" | "running";
  error?: { reason: string; message: string };
  retryable?: boolean;
}

interface Options {
  body: (resultId: string) => string;
  filename?: (resultId: string) => string | undefined;
  contentType?: string;
  requests?: DownloadRequest[];
  tickets?: number[];
  hold?: () => Promise<void>;
  outcome?: (resultId: string) => JobOutcome;
  ticket?: (jobId: number) => Response | undefined;
}

export const jobs = new Map<number, System.Jobs & { result_id: string }>();
let nextJob = 100;

export function emitJob(jobId: number) {
  const reducer = createDefaultReducer().find((item) => item.key === "jobs");
  const job = jobs.get(jobId);
  if (!reducer || !job) return;
  act(() =>
    (reducer.update as (payload: unknown[]) => void)([
      { job_id: jobId, status: job.status, progress_value: null },
    ]),
  );
}

export function setJob(jobId: number, outcome: JobOutcome) {
  const job = jobs.get(jobId)!;
  jobs.set(jobId, {
    ...job,
    status: outcome.status,
    error: outcome.error ?? null,
    retryable: outcome.retryable ?? true,
    progress_message: outcome.status === "completed" ? "Ready to save" : "",
    action:
      outcome.status === "completed"
        ? {
            kind: "discover.save",
            label: "Save",
            ticket: jobId,
            filename: `${job.result_id}.srt`,
          }
        : null,
  });
}

function create(resultId: string, retryOf: number | null = null) {
  const jobId = ++nextJob;
  jobs.set(jobId, {
    job_id: jobId,
    job_name: `Download ${resultId}`,
    status: "pending",
    last_run_time: new Date().toISOString(),
    is_progress: false,
    is_signalr: false,
    progress_value: 0,
    progress_max: 0,
    progress_message: "",
    error: null,
    action: null,
    retryable: true,
    retry_of: retryOf,
    result_id: resultId,
  });
  return jobId;
}

function finish(jobId: number, outcome: JobOutcome) {
  setJob(jobId, outcome);
  // After the enqueue response, the way the socket event follows it.
  setTimeout(() => emitJob(jobId), 0);
}

export function downloadJobHandlers(options: Options) {
  const outcome = options.outcome ?? (() => ({ status: "completed" as const }));
  return [
    http.post("/api/discover/download", async ({ request }) => {
      const body = (await request.json()) as {
        result_id?: string;
        search_id?: string;
      };
      options.requests?.push({
        result: body.result_id ?? null,
        search: body.search_id ?? null,
        authenticated: !!request.headers.get("X-API-KEY"),
      });
      if (options.hold) await options.hold();
      const jobId = create(body.result_id ?? "");
      const result = outcome(body.result_id ?? "");
      if (result.status !== "running") finish(jobId, result);
      return HttpResponse.json({ job_id: jobId }, { status: 202 });
    }),
    http.get("/api/system/jobs", ({ request }) => {
      const id = Number(new URL(request.url).searchParams.get("id"));
      const data = id
        ? [jobs.get(id)].filter(Boolean)
        : Array.from(jobs.values());
      return HttpResponse.json({ data });
    }),
    http.post("/api/system/jobs", ({ request }) => {
      const params = new URL(request.url).searchParams;
      const failed = jobs.get(Number(params.get("id")));
      if (params.get("action") !== "retry" || !failed)
        return new HttpResponse(null, { status: 204 });
      const jobId = create(failed.result_id, failed.job_id);
      finish(jobId, { status: "completed" });
      return HttpResponse.json({ job_id: jobId });
    }),
    http.get("/api/discover/download", ({ request }) => {
      const ticket = Number(new URL(request.url).searchParams.get("job"));
      options.tickets?.push(ticket);
      const override = options.ticket?.(ticket);
      if (override) return override;
      const job = jobs.get(ticket);
      if (!job)
        return HttpResponse.json(
          {
            message:
              "This download is no longer available. Download it again from the results.",
            reason: "ticket_expired",
          },
          { status: 404 },
        );
      const filename = options.filename?.(job.result_id);
      return new HttpResponse(options.body(job.result_id), {
        headers: {
          "Content-Type": options.contentType ?? "application/x-subrip",
          ...(filename
            ? { "Content-Disposition": `attachment; filename="${filename}"` }
            : {}),
        },
      });
    }),
  ];
}

/** The standard job notification whose title names the job. */
export async function findJobToast(name: string) {
  let found: HTMLElement | undefined;
  await waitFor(() => {
    found = screen
      .getAllByRole("alert")
      .find((alert) => within(alert).queryByText(name));
    if (!found) throw new Error(`No notification for ${name}`);
  });
  return found!;
}
