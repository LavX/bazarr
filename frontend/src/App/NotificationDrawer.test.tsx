/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { cleanNotifications } from "@mantine/notifications";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import NotificationDrawer from "@/App/NotificationDrawer";
import { AllProviders } from "@/providers";
import { rawRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";

let nextId = 6000;

function job(overrides: Partial<System.Jobs>): System.Jobs {
  return {
    job_id: ++nextId,
    job_name: `Example job ${nextId}`,
    status: "completed",
    last_run_time: new Date().toISOString(),
    is_progress: false,
    is_signalr: false,
    progress_value: 0,
    progress_max: 0,
    progress_message: "",
    error: null,
    action: null,
    retryable: false,
    retry_of: null,
    ...overrides,
  };
}

async function openDrawer(...jobs: System.Jobs[]) {
  server.use(
    http.get("/api/system/jobs", () => HttpResponse.json({ data: jobs })),
  );
  queryClient.setQueryData([QueryKeys.System, QueryKeys.Jobs], jobs);
  rawRender(
    <AllProviders>
      <NotificationDrawer opened onClose={() => undefined} />
    </AllProviders>,
  );
  const drawer = await screen.findByRole("dialog");
  for (const entry of jobs) {
    expect(within(drawer).getByText(entry.job_name)).toBeInTheDocument();
  }
  return drawer;
}

// The ring labels its percentage, so a percentage label is the ring.
const ringPercent = (drawer: HTMLElement) =>
  within(drawer).queryByText(/^\d+%$/);

beforeEach(() => cleanNotifications());

describe("NotificationDrawer job cards", () => {
  it("renders the completed ring for a job that never reported progress", async () => {
    const drawer = await openDrawer(
      job({ job_name: "Synced sports library with Sportarr (Main)" }),
    );
    expect(within(drawer).getByText("100%")).toBeInTheDocument();
  });

  it("still renders the completed ring for a progress job with counts", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Synced series with Sonarr",
        is_progress: true,
        progress_value: 286,
        progress_max: 286,
      }),
    );
    expect(within(drawer).getByText("100%")).toBeInTheDocument();
  });

  it("renders no ring for a running job that never reported progress", async () => {
    const drawer = await openDrawer(job({ status: "running" }));
    expect(ringPercent(drawer)).toBeNull();
  });

  it("renders the summary a completed job left as its progress message", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Synced sports library with Sportarr (Main)",
        progress_message: "3 leagues synced",
      }),
    );
    expect(within(drawer).getByText("3 leagues synced")).toBeInTheDocument();
  });

  it("renders no ring for a pending job", async () => {
    const drawer = await openDrawer(
      job({
        status: "pending",
        is_progress: true,
        progress_value: 3,
        progress_max: 286,
      }),
    );
    expect(ringPercent(drawer)).toBeNull();
  });

  it("renders no ring for a stopped job that never reported progress", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Syncing sports library with Sportarr (Main)",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
    );
    expect(ringPercent(drawer)).toBeNull();
    expect(within(drawer).getByText("Cancelled by user")).toBeInTheDocument();
  });

  it("still renders the partial ring a stopped progress job earned", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Syncing series with Sonarr",
        is_progress: true,
        stopped: true,
        progress_value: 40,
        progress_max: 286,
      }),
    );
    expect(within(drawer).getByText("14%")).toBeInTheDocument();
  });
});
