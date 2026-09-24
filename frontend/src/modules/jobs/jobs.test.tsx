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
import {
  claimJobCompletion,
  dismissJobOutcome,
  notifyJobOutcome,
  registerJobAction,
  resetJobNotifications,
} from ".";

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

it("leaves a completion its feature handles unannounced, and keeps its action in the Jobs drawer", async () => {
  const handler = vi.fn();
  const handled = new Set<number>();
  const unregister = registerJobAction("example.save", handler, {
    handlesCompletion: (candidate) => handled.has(candidate.job_id),
  });
  const own = job({
    progress_message: "Ready to save",
    action: { kind: "example.save", label: "Save", ticket: 1 },
  });
  const earlier = job({
    progress_message: "Ready to save",
    action: { kind: "example.save", label: "Save", ticket: 2 },
  });
  handled.add(own.job_id);
  server.use(
    http.get("/api/system/jobs", () =>
      HttpResponse.json({ data: [own, earlier] }),
    ),
  );
  queryClient.setQueryData([QueryKeys.System, QueryKeys.Jobs], [own, earlier]);
  rawRender(
    <AllProviders>
      <NotificationDrawer opened onClose={() => undefined} />
    </AllProviders>,
  );
  act(() => {
    notifyJobOutcome(own);
    notifyJobOutcome(earlier);
  });
  // Only the job nobody is handling is announced with its action.
  await screen.findByText(earlier.job_name, { selector: "[role=alert] *" });
  expect(
    screen
      .getAllByRole("alert")
      .some((alert) => within(alert).queryByText(own.job_name)),
  ).toBe(false);
  const drawer = await screen.findByRole("dialog");
  const saves = within(drawer).getAllByRole("button", { name: "Save" });
  expect(saves).toHaveLength(2);
  const user = userEvent.setup();
  await user.click(saves[0]);
  await waitFor(() => expect(handler).toHaveBeenCalledTimes(1));
  unregister();
});

it("takes back a completion announced before its feature claimed the job", async () => {
  const unregister = registerJobAction("example.late", vi.fn());
  rawRender(<AllProviders>{null}</AllProviders>);
  const done = job({
    progress_message: "Ready",
    action: { kind: "example.late", label: "Open" },
  });
  act(() => notifyJobOutcome(done));
  await screen.findByText(done.job_name);
  act(() => dismissJobOutcome(done.job_id));
  await waitFor(() => expect(screen.queryByText(done.job_name)).toBeNull());
  // And it is not announced again.
  act(() => notifyJobOutcome(done));
  expect(screen.queryByText(done.job_name)).toBeNull();
  unregister();
});

it("claims a completion once until the job ids restart with a new socket session", () => {
  const id = ++nextId;
  expect(claimJobCompletion(id)).toBe(true);
  expect(claimJobCompletion(id)).toBe(false);
  resetJobNotifications();
  expect(claimJobCompletion(id)).toBe(true);
});
