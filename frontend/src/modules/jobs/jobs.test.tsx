/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { cleanNotifications } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import NotificationDrawer from "@/App/NotificationDrawer";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { notifyJobOutcome, registerJobAction } from ".";

let nextId = 5000;

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

function toast(name: string) {
  return screen
    .getAllByRole("alert")
    .find((alert) => within(alert).queryByText(name))!;
}

beforeEach(() => cleanNotifications());

describe("standard job outcome notification", () => {
  it("announces a failure once with its message, and a routine completion not at all", async () => {
    rawRender(<AllProviders>{null}</AllProviders>);
    const failed = job({
      status: "failed",
      error: { reason: "timeout", message: "The provider took too long." },
    });
    act(() => {
      notifyJobOutcome(failed);
      notifyJobOutcome(failed);
      notifyJobOutcome(job({ status: "completed" }));
    });
    expect(await screen.findByText(failed.job_name)).toBeInTheDocument();
    expect(screen.getAllByText(failed.job_name)).toHaveLength(1);
    expect(
      within(toast(failed.job_name)).getByText("The provider took too long."),
    ).toBeInTheDocument();
    // Not retryable, so no Retry is offered.
    expect(
      within(toast(failed.job_name)).queryByRole("button", { name: "Retry" }),
    ).toBeNull();
    expect(screen.queryByText(/Example job .* completed/)).toBeNull();
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  });

  it("offers a completed job's action through the handler registered for its kind", async () => {
    const handler = vi.fn();
    const unregister = registerJobAction("example.view", handler);
    rawRender(<AllProviders>{null}</AllProviders>);
    const done = job({
      progress_message: "Ready",
      action: { kind: "example.view", label: "View", id: 7 },
    });
    act(() => notifyJobOutcome(done));
    const user = userEvent.setup();
    await screen.findByText(done.job_name);
    await user.click(
      within(toast(done.job_name)).getByRole("button", { name: "View" }),
    );
    expect(handler).toHaveBeenCalledWith(done.action, done);
    unregister();
    // An action nobody can run is not offered.
    const orphan = job({ action: { kind: "example.view", label: "View" } });
    act(() => notifyJobOutcome(orphan));
    expect(screen.queryByText(orphan.job_name)).toBeNull();
  });

  it("retries a retryable failure through the standard jobs API", async () => {
    const retried: string[] = [];
    server.use(
      http.post("/api/system/jobs", ({ request }) => {
        const params = new URL(request.url).searchParams;
        retried.push(`${params.get("id")}:${params.get("action")}`);
        return HttpResponse.json({ job_id: 1 });
      }),
    );
    rawRender(<AllProviders>{null}</AllProviders>);
    const failed = job({
      status: "failed",
      retryable: true,
      error: { reason: "provider_error", message: "Provider error." },
    });
    act(() => notifyJobOutcome(failed));
    const user = userEvent.setup();
    await screen.findByText(failed.job_name);
    await user.click(
      within(toast(failed.job_name)).getByRole("button", { name: "Retry" }),
    );
    await waitFor(() => expect(retried).toEqual([`${failed.job_id}:retry`]));
  });
});

it("shows a failed job's error and its Retry in the Jobs drawer", async () => {
  const failed = job({
    status: "failed",
    retryable: true,
    error: { reason: "invalid_subtitle", message: "Not a usable subtitle." },
  });
  server.use(
    http.get("/api/system/jobs", () => HttpResponse.json({ data: [failed] })),
  );
  queryClient.setQueryData([QueryKeys.System, QueryKeys.Jobs], [failed]);
  rawRender(
    <AllProviders>
      <NotificationDrawer opened onClose={() => undefined} />
    </AllProviders>,
  );
  const drawer = await screen.findByRole("dialog");
  expect(
    within(drawer).getByText("Not a usable subtitle."),
  ).toBeInTheDocument();
  expect(within(drawer).getByRole("button", { name: "Retry" })).toBeEnabled();
});
