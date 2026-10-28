/* eslint-disable camelcase -- API fields keep their transport names. */
import { http, HttpResponse, type HttpResponseResolver } from "msw";

// The Discover search is a standard job: the POST answers 202 with the job it
// queued, and the providers heard from and finally the snapshot are the job's
// result, read from system/jobs/result. Most Discover tests care about what the
// page does with a search's outcome, not about the queue, so they describe the
// search the way the old route answered, as a snapshot, and this turns it into
// the job protocol: the POST holds until the described answer is ready, then
// queues a job that has already finished with it. A refusal (a 4xx or 5xx, or a
// dropped connection) is passed through untouched, because the route answers
// those before it queues anything.

interface FinishedSearch {
  status: "completed";
  result: unknown;
}

const finishedSearches = new Map<number, FinishedSearch>();
let nextJobId = 9000;

export const SEARCH_SESSION = "run-discover-test";

/** Answer `POST /api/discover/search` as the job protocol, from an old-style resolver. */
export function discoverSearchJob(resolver: HttpResponseResolver) {
  return http.post("/api/discover/search", async (info) => {
    const response = await resolver(info);
    if (!(response instanceof Response)) return response;
    if (response.type === "error" || response.status !== 200) return response;
    const snapshot = (await response.clone().json()) as {
      coverage?: { providers?: unknown[] };
    };
    const jobId = ++nextJobId;
    finishedSearches.set(jobId, {
      status: "completed",
      result: {
        phase: "finished",
        providers: snapshot.coverage?.providers ?? [],
        snapshot,
      },
    });
    return HttpResponse.json(
      { job_id: jobId, session: SEARCH_SESSION },
      { status: 202 },
    );
  });
}

/** The default `system/jobs/result` answer for the searches queued above. */
export const searchJobResultHandler = http.get(
  "/api/system/jobs/result",
  ({ request }) => {
    const id = Number(new URL(request.url).searchParams.get("id"));
    const finished = finishedSearches.get(id);
    if (finished === undefined) {
      return HttpResponse.json(
        { message: "This job is no longer listed." },
        { status: 404 },
      );
    }
    return HttpResponse.json({
      job_id: id,
      session: SEARCH_SESSION,
      status: finished.status,
      error: null,
      result: finished.result,
    });
  },
);
