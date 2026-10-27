/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { isJobCompletionClaimed, observeJobSession } from "@/modules/jobs";
import TimingToolsPanel from "@/pages/SubtitleEditor/TimingToolsPanel";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// A sync's job id only means something within the backend run that queued it.
// The panel binds its claim to that run and, when it gives up on a sync, only
// speaks for a job of the same run.

const SUBMITTED_UNDER = "run-submitted";

function finishedJob(session: string) {
  return {
    job_id: 77,
    session,
    job_name: "Editor Sync",
    status: "completed",
    last_run_time: new Date().toISOString(),
    is_progress: true,
    is_signalr: false,
    progress_value: 3,
    progress_max: 3,
    progress_message: "Sync complete",
    error: null,
    action: null,
    retryable: false,
    retry_of: null,
    origin: "user",
    stopped: false,
  };
}

async function runLostSync(recheckedAs: string) {
  const jobs = vi.fn(() =>
    HttpResponse.json({ data: [finishedJob(recheckedAs)] }),
  );
  server.use(
    http.post("/api/editor/sync", () =>
      HttpResponse.json(
        {
          jobKey: "job 1",
          jobId: 77,
          jobSession: SUBMITTED_UNDER,
          status: "running",
        },
        { status: 202 },
      ),
    ),
    http.get("/api/editor/sync", () =>
      HttpResponse.json({ status: "not_found" }, { status: 404 }),
    ),
    http.get("/api/system/jobs", jobs),
  );
  customRender(
    <TimingToolsPanel
      open
      cues={[]}
      selectedIndices={new Set()}
      mediaType="movie"
      mediaId={50}
      arrInstanceId={3}
      language="en"
      onApplyBatch={vi.fn()}
      onGetContent={() => ({
        content: "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
        format: "srt",
        encoding: "utf-8",
      })}
      onApplySyncedContent={vi.fn()}
      onClose={vi.fn()}
    />,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Auto-Sync" }));
  await user.click(screen.getByRole("button", { name: "Sync" }));
  await screen.findByText(/Sync job lost/);
  await waitFor(() => expect(jobs).toHaveBeenCalled());
}

beforeEach(() => {
  vi.stubGlobal("Bazarr", { apiKey: "key", baseUrl: "" });
  observeJobSession(SUBMITTED_UNDER);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("editor sync and the backend session", () => {
  it("announces the finish of the sync it gave up on when the same backend run still has it", async () => {
    await runLostSync(SUBMITTED_UNDER);
    await screen.findByText("Editor Sync");
  });

  it("stays silent about a new job that reuses the id after a restart", async () => {
    await runLostSync("run-restarted");
    // Give the recovery its moment, then check that nothing was announced for
    // the unrelated job.
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(screen.queryByText("Editor Sync")).toBeNull();
    expect(isJobCompletionClaimed(77, "run-restarted")).toBe(false);
  });
});
