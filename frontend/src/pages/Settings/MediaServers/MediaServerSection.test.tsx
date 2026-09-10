/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import queryClient from "@/apis/queries";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import { act, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const id = "2af88684-d7d2-4534-82bb-a6d839cc5c10";
const secondId = "66e883df-68c7-4d66-8217-8fc206c5a563";
const item = `/api/system/media-server-instances/${id}`;
function instance(
  kind: MediaServerKind,
  overrides: Partial<MediaServerInstance> = {},
): MediaServerInstance {
  return {
    id,
    kind,
    name: "Living room",
    enabled: true,
    url: `https://${kind}.example`,
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [
      {
        local_path: "/movies",
        remote_path: "/media/movies",
        ...(kind === "silo" ? { library_id: "0007" } : {}),
      },
    ],
    ...overrides,
  };
}
function setup(kind: MediaServerKind, enabled = true, rows = [instance(kind)]) {
  window.history.replaceState(null, "", `/#${kind}`);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", [`use_${kind}`]: enabled },
      }),
    ),
    http.get("/api/system/media-server-instances", () =>
      HttpResponse.json({ data: rows }),
    ),
    http.get("/api/system/media-server-instances/:id/status", () =>
      HttpResponse.json({ pending: 0, state: "idle", error_code: null }),
    ),
  );
  customRender(<SettingsConnectionsView />);
}
async function edit() {
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  return screen.findByRole("dialog");
}
afterEach(() => window.history.replaceState(null, "", "/"));

it.each(["emby", "silo"] as const)(
  "edits %s with saved credentials and keeps dirty global settings through instance save",
  async (kind) => {
    const writes: unknown[] = [];
    let testBody: unknown;
    let settingsReads = 0;
    setup(kind, false);
    server.use(
      http.get("/api/system/settings", () => {
        settingsReads++;
        return HttpResponse.json({
          general: { theme: "auto", [`use_${kind}`]: false },
        });
      }),
      http.patch(item, async ({ request }) => {
        const body = await request.json();
        writes.push(body);
        return HttpResponse.json({ ...instance(kind), ...(body as object) });
      }),
      http.post(`${item}/test-connection`, async ({ request }) => {
        testBody = await request.json();
        return HttpResponse.json({ success: true, server_name: "Living room" });
      }),
      http.post("/api/system/settings", () => {
        writes.push("global-save");
        return HttpResponse.json({});
      }),
    );
    await screen.findByRole("button", { name: "Edit" });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await userEvent.click(screen.getByRole("switch", { name: "Enabled" }));
    const form = within(await edit());
    await userEvent.clear(form.getByLabelText("Server URL"));
    await userEvent.type(
      form.getByLabelText("Server URL"),
      `https://${kind}.example/prefix`,
    );
    await userEvent.click(
      form.getByRole("switch", { name: "Verify SSL certificate" }),
    );
    await userEvent.click(form.getByRole("button", { name: "Test" }));
    expect(
      await form.findByText("Connected to Living room."),
    ).not.toHaveTextContent(/refresh permission|subtitle/i);
    expect(
      form.getAllByText("Test checks access, not refresh permission."),
    ).toHaveLength(1);
    expect(form.queryByText(/vundefined/)).not.toBeInTheDocument();
    expect(testBody).toEqual({
      url: `https://${kind}.example/prefix`,
      verify_ssl: false,
    });
    const beforeSave = settingsReads;
    await userEvent.click(form.getByRole("button", { name: "Save instance" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(writes).toHaveLength(1);
    expect(writes[0]).toMatchObject({
      url: `https://${kind}.example/prefix`,
      verify_ssl: false,
    });
    expect(writes[0]).not.toHaveProperty("api_key");
    expect(settingsReads).toBe(beforeSave);
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
    expect(
      screen.getByRole("button", { name: /Save 1 pending change/ }),
    ).toBeInTheDocument();
  },
);

it.each(["emby", "silo"] as const)(
  "shows two %s cards and scopes toggle, edit, delete, status and retry to their UUIDs",
  async (kind) => {
    const rows = [
      instance(kind),
      instance(kind, { id: secondId, name: "Bedroom" }),
    ];
    const writes: unknown[] = [];
    let retried = false;
    setup(kind, true, rows);
    server.use(
      http.patch(item, async ({ request }) => {
        const body = (await request.json()) as Partial<MediaServerInstance>;
        writes.push([id, body]);
        Object.assign(rows[0], body);
        return HttpResponse.json(rows[0]);
      }),
      http.get(`${item}/status`, () =>
        HttpResponse.json({
          pending: retried ? 0 : 2,
          state: retried ? "requested" : "unconfirmed",
          error_code: null,
        }),
      ),
      http.post(`${item}/retry-pending`, () => {
        writes.push([id, "retry"]);
        retried = true;
        return HttpResponse.json({ queued: 2 });
      }),
      http.delete(item, () => {
        writes.push([id, "delete"]);
        rows.shift();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const first = within(
      await screen.findByRole("region", { name: "Living room" }),
    );
    const second = within(screen.getByRole("region", { name: "Bedroom" }));
    expect(await first.findByText(/2 refreshes queued/)).toBeInTheDocument();
    expect(await second.findByText(/No pending refreshes/)).toBeInTheDocument();
    await userEvent.click(first.getByRole("button", { name: "Retry pending" }));
    await first.findByText("Queued 2 refreshes.");
    await userEvent.click(
      first.getByRole("switch", { name: "Disable instance" }),
    );
    await waitFor(() =>
      expect(
        first.getByRole("switch", { name: "Enable instance" }),
      ).not.toBeChecked(),
    );
    expect(
      second.getByRole("switch", { name: "Disable instance" }),
    ).toBeChecked();
    await userEvent.click(first.getByRole("button", { name: "Edit" }));
    const modal = within(await screen.findByRole("dialog"));
    await userEvent.clear(modal.getByLabelText("Name"));
    await userEvent.type(modal.getByLabelText("Name"), "Renamed");
    await userEvent.click(modal.getByRole("button", { name: "Save instance" }));
    const renamed = within(
      await screen.findByRole("region", { name: "Renamed" }),
    );
    await userEvent.click(renamed.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Cancel",
      }),
    );
    expect(writes).toHaveLength(3);
    await userEvent.click(renamed.getByRole("button", { name: "Delete" }));
    await userEvent.click(
      within(await screen.findByRole("dialog")).getByRole("button", {
        name: "Delete instance",
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "Renamed" }),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("region", { name: "Bedroom" })).toBeInTheDocument();
    expect(writes).toHaveLength(4);
    expect(
      queryClient.getQueryData(["media-servers", kind, id, "status"]),
    ).toBeUndefined();
  },
);

it("shows safe Test failures, resets on edits and disables probes when clearing a saved key", async () => {
  setup("emby");
  server.use(
    http.post(`${item}/test-connection`, () =>
      HttpResponse.json({ success: false, error_code: "authentication" }),
    ),
  );
  const form = within(await edit());
  await userEvent.click(form.getByRole("button", { name: "Test" }));
  await form.findByText(/Connection test failed/);
  await userEvent.type(form.getByLabelText("Server URL"), "/changed");
  expect(form.queryByText(/Connection test failed/)).not.toBeInTheDocument();
  await userEvent.click(form.getByRole("radio", { name: "Clear" }));
  expect(form.getByRole("button", { name: "Test" })).toBeDisabled();
});

it("shows HTTP Test failures without displaying raw errors", async () => {
  setup("silo");
  server.use(
    http.post(`${item}/test-connection`, () =>
      HttpResponse.json({}, { status: 502 }),
    ),
  );
  const form = within(await edit());
  await userEvent.click(form.getByRole("button", { name: "Test" }));
  await form.findByText(/Connection test failed/);
});

it("loads saved-key Silo libraries with current fields and distinguishes empty from failure", async () => {
  let body: unknown;
  setup("silo");
  server.use(
    http.post(`${item}/libraries`, async ({ request }) => {
      body = await request.json();
      return HttpResponse.json({ data: [], error_code: null });
    }),
  );
  const form = within(await edit());
  await userEvent.click(form.getByRole("radio", { name: "Replace" }));
  await userEvent.type(form.getByLabelText("API Key"), "0007");
  await userEvent.click(form.getByRole("button", { name: "Load libraries" }));
  await form.findByText(/No supported libraries found/);
  expect(body).toEqual({
    url: "https://silo.example",
    api_key: "0007",
    verify_ssl: true,
  });
  server.use(
    http.post(`${item}/libraries`, () =>
      HttpResponse.json({ data: [], error_code: "connection_error" }),
    ),
  );
  await userEvent.click(form.getByRole("button", { name: "Load libraries" }));
  await form.findByText(/Could not load Silo libraries/);
  expect(
    form.queryByText(/No supported libraries found/),
  ).not.toBeInTheDocument();
});

it("treats missing status as unavailable and keeps retry disabled", async () => {
  setup("silo");
  server.use(
    http.get(`${item}/status`, () => HttpResponse.json({}, { status: 404 })),
  );
  expect(
    await screen.findByText(/Refresh status unavailable/),
  ).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry pending" })).toBeDisabled();
  expect(screen.queryByText(/No pending refreshes/)).not.toBeInTheDocument();
});

it("warns about unconfirmed refreshes, retries pending work and refreshes status", async () => {
  setup("silo");
  let retried = false;
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({
        pending: retried ? 0 : 2,
        state: retried ? "requested" : "unconfirmed",
        error_code: retried ? null : "timeout",
      }),
    ),
    http.post(`${item}/retry-pending`, () => {
      retried = true;
      return HttpResponse.json({ queued: 2 });
    }),
  );
  expect(await screen.findByText(/Refresh unconfirmed/)).toBeInTheDocument();
  expect(screen.getByText(/2 refreshes queued/)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: "Retry pending" }));
  expect(await screen.findByText("Queued 2 refreshes.")).toBeInTheDocument();
  expect(await screen.findByText(/Refresh sent/)).toBeInTheDocument();
  expect(
    screen.getAllByText(
      "Confirmation means the server finished a scan, not that it found the subtitle.",
    ),
  ).toHaveLength(1);
});

it("disables retry when master changes are staged and shows retry failures safely", async () => {
  setup("emby");
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({
        pending: 1,
        state: "pending",
        error_code: "connection_error",
      }),
    ),
    http.post(`${item}/retry-pending`, () =>
      HttpResponse.json({}, { status: 502 }),
    ),
  );
  const retry = await screen.findByRole("button", { name: "Retry pending" });
  await waitFor(() => expect(retry).toBeEnabled());
  await userEvent.click(retry);
  expect(
    await screen.findByText(/Could not queue pending refreshes/),
  ).toBeInTheDocument();
  await userEvent.click(screen.getByRole("switch", { name: "Enabled" }));
  expect(retry).toBeDisabled();
  expect(
    screen.getByText(/Save your changes before retrying/),
  ).toBeInTheDocument();
});

it("does not claim there is no pending work when an idle status includes queued changes", async () => {
  setup("emby");
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({ pending: 2, state: "idle", error_code: null }),
    ),
  );
  expect(await screen.findByText(/2 refreshes queued/)).toBeInTheDocument();
  expect(screen.queryByText(/No pending refreshes/)).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Retry pending" })).toBeEnabled();
});

it("counts a single queued refresh in the singular", async () => {
  setup("emby");
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({ pending: 1, state: "pending", error_code: null }),
    ),
    http.post(`${item}/retry-pending`, () => HttpResponse.json({ queued: 1 })),
  );
  expect(await screen.findByText(/1 refresh queued\./)).toBeInTheDocument();
  expect(screen.queryByText(/1 refreshes/)).not.toBeInTheDocument();
  const retry = screen.getByRole("button", { name: "Retry pending" });
  await waitFor(() => expect(retry).toBeEnabled());
  await userEvent.click(retry);
  expect(await screen.findByText("Queued 1 refresh.")).toBeInTheDocument();
});

it.each(["confirmed", "requested", "idle"] as const)(
  "keeps overflow actionable when the status is %s with no retained work",
  async (state) => {
    setup("silo");
    server.use(
      http.get(`${item}/status`, () =>
        HttpResponse.json({ pending: 0, state, error_code: "queue_overflow" }),
      ),
    );
    const warning = await screen.findByText(/refresh queue overflowed/i);
    expect(warning).toHaveTextContent(
      /Retry pending covers the refreshes it kept/i,
    );
    expect(warning).toHaveTextContent(/dropped ones need a new refresh/i);
    expect(warning).not.toHaveTextContent(
      /Refresh confirmed|Refresh sent|No pending refreshes|0 refresh/i,
    );
    expect(
      screen.getByRole("button", { name: "Retry pending" }),
    ).toBeDisabled();
  },
);

it("retries retained overflow targets and keeps the dropped-target warning after they drain", async () => {
  setup("silo");
  let retried = false;
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({
        pending: retried ? 0 : 2,
        state: retried ? "confirmed" : "unconfirmed",
        error_code: "queue_overflow",
      }),
    ),
    http.post(`${item}/retry-pending`, () => {
      retried = true;
      return HttpResponse.json({ queued: 2 });
    }),
  );
  expect(await screen.findByText(/2 refreshes queued/)).toHaveTextContent(
    /refresh queue overflowed/i,
  );
  const retry = screen.getByRole("button", { name: "Retry pending" });
  expect(retry).toBeEnabled();
  await userEvent.click(retry);
  expect(await screen.findByText("Queued 2 refreshes.")).toBeInTheDocument();
  const warning = await screen.findByText(/refresh queue overflowed/i);
  expect(warning).toHaveTextContent(/dropped ones need a new refresh/i);
  expect(warning).not.toHaveTextContent(/Refresh confirmed|0 refresh/i);
  expect(retry).toBeDisabled();
});

it("explains that an unsupported Silo subtitle location needs to change before retrying", async () => {
  setup("silo");
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({
        pending: 1,
        state: "unconfirmed",
        error_code: "sidecar_unsupported",
        message: "Unsupported subtitle: /private/subtitles/movie.srt",
      }),
    ),
  );
  const warning = await screen.findByText(/1 refresh queued/);
  expect(warning).toHaveTextContent(/subtitles stored beside the video file/i);
  expect(warning).toHaveTextContent(/Move the subtitle there, then retry/i);
  expect(warning).not.toHaveTextContent(
    /Refresh unconfirmed|Check the saved connection|private|movie\.srt/i,
  );
  expect(screen.getByRole("button", { name: "Retry pending" })).toBeEnabled();
});

it("keeps unknown status errors generic without exposing the raw error", async () => {
  setup("emby");
  server.use(
    http.get(`${item}/status`, () =>
      HttpResponse.json({
        pending: 1,
        state: "unconfirmed",
        error_code: "unknown_error: /private/subtitles/movie.srt",
      }),
    ),
  );
  const warning = await screen.findByText(/1 refresh queued/);
  expect(warning).toHaveTextContent(/Refresh unconfirmed/i);
  expect(warning).toHaveTextContent(
    /Check the saved connection, server access and path mappings/i,
  );
  expect(warning).not.toHaveTextContent(/unknown_error|private|movie\.srt/i);
});

it.each(["emby", "silo"] as const)(
  "adds a disabled %s instance with an unsaved string key, and Cancel performs no write",
  async (kind) => {
    const writes: unknown[] = [];
    let probe: unknown;
    setup(kind, false, []);
    server.use(
      http.post(`/api/${kind}/test-connection`, async ({ request }) => {
        probe = await request.json();
        return HttpResponse.json({ success: true });
      }),
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = (await request.json()) as LooseObject;
        writes.push(body);
        return HttpResponse.json(
          instance(kind, { ...body, api_key_set: true }),
        );
      }),
      http.post("/api/system/settings", () => {
        writes.push("global-save");
        return HttpResponse.json({});
      }),
    );
    await screen.findByText(
      `No ${kind === "emby" ? "Emby" : "Silo"} instances yet`,
    );
    await userEvent.click(screen.getByRole("button", { name: "Add instance" }));
    let modal = within(await screen.findByRole("dialog"));
    expect(
      modal.getByRole("switch", { name: "Instance enabled" }),
    ).not.toBeChecked();
    expect(
      modal.getByRole("switch", { name: "Verify SSL certificate" }),
    ).toBeChecked();
    expect(modal.getByRole("button", { name: "Test" })).toBeDisabled();
    await userEvent.type(modal.getByLabelText("Name"), "Cancelled");
    await userEvent.type(modal.getByLabelText("API Key"), "cancelled-key");
    await userEvent.click(modal.getByRole("button", { name: "Cancel" }));
    expect(writes).toEqual([]);
    await userEvent.click(screen.getByRole("button", { name: "Add instance" }));
    modal = within(await screen.findByRole("dialog"));
    expect(modal.getByLabelText("Name")).toHaveValue("");
    expect(modal.getByLabelText("API Key")).toHaveValue("");
    await userEvent.type(modal.getByLabelText("Name"), "New room");
    await userEvent.type(
      modal.getByLabelText("Server URL"),
      `https://${kind}.example/prefix`,
    );
    await userEvent.type(modal.getByLabelText("API Key"), "0007");
    await userEvent.click(
      modal.getByRole("switch", { name: "Verify SSL certificate" }),
    );
    await userEvent.click(modal.getByRole("button", { name: "Test" }));
    await modal.findByText("Connection succeeded.");
    expect(probe).toEqual({
      url: `https://${kind}.example/prefix`,
      apikey: "0007",
      verify_ssl: false,
    });
    await userEvent.click(modal.getByRole("button", { name: "Save instance" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(writes).toEqual([
      {
        kind,
        name: "New room",
        url: `https://${kind}.example/prefix`,
        api_key: "0007",
        enabled: false,
        verify_ssl: false,
        path_mappings: [],
      },
    ]);
  },
);

it("keeps a failed save open, saves explicit clear, and handles Enter and Ctrl+S within the instance form", async () => {
  setup("emby", false);
  const writes: LooseObject[] = [];
  let fail = true;
  let globalWrites = 0;
  server.use(
    http.patch(item, async ({ request }) => {
      const body = (await request.json()) as LooseObject;
      writes.push(body);
      return fail
        ? HttpResponse.json({}, { status: 502 })
        : HttpResponse.json(instance("emby", { ...body, api_key_set: false }));
    }),
    http.post("/api/system/settings", () => {
      globalWrites++;
      return HttpResponse.json({});
    }),
  );
  await screen.findByRole("button", { name: "Edit" });
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  await userEvent.click(screen.getByRole("switch", { name: "Enabled" }));
  const modal = within(await edit());
  await userEvent.click(modal.getByRole("radio", { name: "Clear" }));
  await userEvent.click(
    modal.getByRole("switch", { name: "Instance enabled" }),
  );
  await userEvent.type(modal.getByLabelText("Name"), "{Enter}");
  await modal.findByText(/Could not save this instance/);
  expect(screen.getByRole("dialog")).toBeInTheDocument();
  expect(writes[0]).toMatchObject({ clear_api_key: true, enabled: false });
  expect(writes[0]).not.toHaveProperty("api_key");
  expect(globalWrites).toBe(0);
  fail = false;
  await userEvent.click(modal.getByLabelText("Name"));
  await userEvent.keyboard("{Control>}s{/Control}");
  await waitFor(() =>
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
  );
  expect(writes).toHaveLength(2);
  expect(globalWrites).toBe(0);
  expect(
    screen.getByRole("button", { name: /Save 1 pending change/ }),
  ).toBeInTheDocument();
});

it.each([
  ["test-connection", "Bedroom"],
  ["libraries", "Bedroom"],
  ["save", "Bedroom"],
  ["test-connection", "Living room"],
  ["libraries", "Living room"],
  ["save", "Living room"],
] as const)(
  "a late %s response cannot change or close the %s editor",
  async (operation, target) => {
    const rows = [
      instance("silo"),
      instance("silo", { id: secondId, name: "Bedroom" }),
    ];
    setup("silo", true, rows);
    let finish: (() => void) | undefined;
    const handler = async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json(
        operation === "test-connection"
          ? { success: true, server_name: "Stale response" }
          : operation === "libraries"
            ? {
                data: [
                  {
                    id: "0007",
                    name: "Stale library",
                    type: "series",
                    paths: ["/tv"],
                  },
                ],
                error_code: null,
              }
            : rows[0],
      );
    };
    server.use(
      operation === "save"
        ? http.patch(item, handler)
        : http.post(`${item}/${operation}`, handler),
    );
    await userEvent.click(
      within(
        await screen.findByRole("region", { name: "Living room" }),
      ).getByRole("button", { name: "Edit" }),
    );
    const first = within(await screen.findByRole("dialog"));
    await userEvent.click(
      first.getByRole("button", {
        name:
          operation === "test-connection"
            ? "Test"
            : operation === "libraries"
              ? "Load libraries"
              : "Save instance",
      }),
    );
    await waitFor(() => expect(finish).toBeDefined());
    await userEvent.click(first.getByRole("button", { name: "Cancel" }));
    await userEvent.click(
      within(screen.getByRole("region", { name: target })).getByRole("button", {
        name: "Edit",
      }),
    );
    await act(async () => finish?.());
    await waitFor(() => expect(queryClient.isMutating()).toBe(0));
    const second = within(await screen.findByRole("dialog"));
    expect(second.getByLabelText("Name")).toHaveValue(target);
    expect(
      second.queryByText(/Stale response|Stale library/),
    ).not.toBeInTheDocument();
    await userEvent.click(second.getByRole("button", { name: "Cancel" }));
    await userEvent.click(
      within(screen.getByRole("region", { name: "Living room" })).getByRole(
        "button",
        { name: "Edit" },
      ),
    );
    const reopened = within(await screen.findByRole("dialog"));
    expect(reopened.getByLabelText("Name")).toHaveValue("Living room");
    expect(
      reopened.queryByText(/Stale response|Stale library/),
    ).not.toBeInTheDocument();
  },
);

it("tests a saved card by UUID using the current server-side connection", async () => {
  let body: unknown;
  setup("emby");
  server.use(
    http.post(`${item}/test-connection`, async ({ request }) => {
      body = await request.json();
      return HttpResponse.json({ success: true, server_name: "Living room" });
    }),
  );
  await userEvent.click(await screen.findByRole("button", { name: "Test" }));
  await screen.findByText("Connected to Living room.");
  expect(body).toEqual({});
});

it("shows an instance loading failure and retries the list without a settings write", async () => {
  setup("emby");
  let fail = true;
  server.use(
    http.get("/api/system/media-server-instances", () =>
      fail
        ? HttpResponse.json({}, { status: 502 })
        : HttpResponse.json({ data: [] }),
    ),
  );
  await screen.findByText("Could not load Emby instances");
  fail = false;
  await userEvent.click(screen.getByRole("button", { name: "Retry" }));
  await screen.findByText("No Emby instances yet");
});

it.each([
  ["emby", "Notify Emby about movie and episode subtitle changes."],
  [
    "silo",
    "Notify Silo about movie and episode subtitle changes. Subtitle sidecars must be beside the video file.",
  ],
] as const)(
  "says every subtitle change reaches %s, not only new ones",
  async (kind, copy) => {
    setup(kind);
    expect(await screen.findByText(copy)).toBeInTheDocument();
    expect(screen.queryByText(/subtitle additions/i)).not.toBeInTheDocument();
  },
);
