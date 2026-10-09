/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { cleanNotifications } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import NotificationDrawer from "@/App/NotificationDrawer";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
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

  it("renders a stopped job under a Stopped heading, not Completed", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Syncing sports library with Sportarr (Main)",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
    );
    expect(
      within(drawer).queryByRole("heading", { name: "Completed" }),
    ).toBeNull();
    expect(
      within(drawer).getByRole("heading", { name: "Stopped" }),
    ).toBeInTheDocument();
  });

  it("counts stopped jobs in their own group, not the Completed one", async () => {
    const drawer = await openDrawer(
      job({ job_name: "Synced series with Sonarr" }),
      job({
        job_name: "Syncing sports library with Sportarr (Main)",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
      job({
        job_name: "Syncing movies with Radarr",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
    );
    expect(within(drawer).getByText("1 job")).toBeInTheDocument();
    expect(within(drawer).getByText("2 jobs")).toBeInTheDocument();
    expect(within(drawer).queryByText("3 jobs")).toBeNull();
  });

  it("keeps a stopped progress job's partial ring in the Stopped group, and none for a job without progress", async () => {
    const drawer = await openDrawer(
      job({
        job_name: "Syncing series with Sonarr",
        is_progress: true,
        stopped: true,
        progress_value: 40,
        progress_max: 286,
      }),
      job({
        job_name: "Syncing sports library with Sportarr (Main)",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
    );
    expect(
      within(drawer).getByRole("heading", { name: "Stopped" }),
    ).toBeInTheDocument();
    expect(within(drawer).getByText("14%")).toBeInTheDocument();
    expect(within(drawer).getAllByText(/^\d+%$/)).toHaveLength(1);
  });

  it("clears the completed queue, where stopped jobs are recorded, from the Stopped group's menu", async () => {
    const user = userEvent.setup();
    let cleared: FormData | undefined;
    server.use(
      http.patch("/api/system/jobs", async ({ request }) => {
        cleared = await request.formData();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const drawer = await openDrawer(
      job({
        job_name: "Syncing sports library with Sportarr (Main)",
        stopped: true,
        progress_message: "Cancelled by user",
      }),
    );
    expect(
      within(drawer).getByRole("heading", { name: "Stopped" }),
    ).toBeInTheDocument();
    // The group's ellipsis menu has no accessible name, and the drawer's
    // other button is the header close, so the menu is found by its icon.
    const groupMenu = within(drawer).getByRole("button", {
      name: (_, element) =>
        // eslint-disable-next-line testing-library/no-node-access
        element.querySelector('svg[data-icon="ellipsis"]') !== null,
    });
    await user.click(groupMenu);
    await user.click(
      await screen.findByRole("menuitem", { name: "Clear this queue" }),
    );
    await waitFor(() => expect(cleared?.get("queueName")).toBe("completed"));
  });
});
