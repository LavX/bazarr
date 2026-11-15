/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type { TranslationState } from "@/types/translation";
import { connection, engine, second, SECRET } from "./fixtures";
import TranslationView from ".";

// The page's longer flows: what stays on screen while a request is on its way,
// and what a failure in the middle leaves behind.

let state: TranslationState;
let calls: Array<{ method: string; path: string; body: unknown }>;

function record(method: string, path: string, body: unknown) {
  calls.push({ method, path, body });
}

/** A response the test lets through when it chooses to. */
function gate() {
  let open: () => void = () => undefined;
  const opened = new Promise<void>((resolve) => {
    open = resolve;
  });
  return { open, opened };
}

beforeEach(() => {
  calls = [];
  window.history.replaceState({}, "", "/");
  state = {
    enabled: true,
    engine: 1,
    fallback_engine: null,
    instances: [second, engine()],
  };
  server.use(
    http.get("/api/translation/state", () => HttpResponse.json(state)),
    http.get("/api/translation/history", () =>
      HttpResponse.json({ data: [], total: 0 }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {},
        translator: {
          default_score: 50,
          min_source_score: 90,
          translator_info: true,
          openrouter_max_concurrent: 2,
        },
      }),
    ),
    http.post("/api/translation/engines/:id/options", () =>
      HttpResponse.json({ available: false, items: [], reason: "none" }),
    ),
  );
});

async function showPage() {
  customRender(<TranslationView />);
  return screen.findByRole("radiogroup", { name: "Engine" });
}

const editNames = () =>
  screen
    .getAllByRole("button", { name: /^Edit / })
    .map((item) => item.getAttribute("aria-label"));

describe("Translation page, first run that fails half way", () => {
  it("keeps the panel and its error when the engine cannot be selected, and a retry only selects it", async () => {
    // A server that keeps what it is sent: the back end exists after the first request.
    let loads = 0;
    let pointerFails = true;
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [engine({ configured: false, connections: [] })],
    };
    server.use(
      http.get("/api/translation/state", () => {
        loads += 1;
        return HttpResponse.json(state);
      }),
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          const created = connection(3, { backend_type: "openrouter" });
          state = {
            ...state,
            instances: [engine({ connections: [created] })],
          };
          return HttpResponse.json({ connection: created }, { status: 201 });
        },
      ),
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        if (pointerFails) {
          return HttpResponse.json(
            { error: "bad_request", message: "The engine is not ready." },
            { status: 400 },
          );
        }
        state = { ...state, engine: 1 };
        return HttpResponse.json({
          engine: 1,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    const before = loads;
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    // The back end was saved and the engine was refused. The panel is still here and says so.
    expect(
      await screen.findByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).toBeVisible();
    expect(calls.map((call) => call.path)).toEqual(["connection", "pointers"]);
    // Nothing was loaded again after the failure.
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(loads).toBe(before);

    // Even a refresh from elsewhere, which now reports a back end, does not take the panel away.
    await queryClient.invalidateQueries();
    await waitFor(() => expect(loads).toBeGreaterThan(before));
    expect(
      screen.getByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    // The key is not asked for again.
    expect(screen.getByLabelText("OpenRouter key")).toBeDisabled();

    pointerFails = false;
    await user.click(
      screen.getByRole("button", { name: "Switch the engine on" }),
    );
    // The retry selected the engine and did not add the back end a second time.
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "connection",
        "pointers",
        "pointers",
      ]),
    );
    // Now that it went through, the page is the configured one.
    const group = await screen.findByRole("radiogroup", { name: "Engine" });
    expect(
      within(group).getByRole("radio", { name: /AI Subtitle Translator/ }),
    ).toBeChecked();
    expect(
      screen.queryByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("Translation page, a conflict answered before the reload has come back", () => {
  it("Discard and reload waits for what is saved now before it replaces the draft and the revision", async () => {
    let saved = "Translated by Bazarr+";
    let revision = 3;
    const held = gate();
    let holding = false;
    const sent: Array<{ expected_revision?: number }> = [];
    server.use(
      http.get("/api/translation/state", async () => {
        if (holding) {
          await held.opened;
        }
        return HttpResponse.json({
          ...state,
          instances: [
            second,
            engine({ revision, values: { credit_line: saved } }),
          ],
        });
      }),
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { expected_revision?: number };
        sent.push(body);
        if (body.expected_revision !== revision) {
          // From here on the state takes its time: the refresh the conflict starts stays open.
          holding = true;
          return HttpResponse.json(
            {
              error: "stale_revision",
              message: "The engine settings changed since they were loaded.",
              revision,
            },
            { status: 409 },
          );
        }
        revision += 1;
        return HttpResponse.json({
          engine: engine({ revision, values: { credit_line: saved } }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    // Someone else saves.
    saved = "Theirs";
    revision = 9;
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Mine");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await screen.findByText(/changed somewhere else/);

    // Clicked at once, while the refresh is still on its way.
    await user.click(
      screen.getByRole("button", { name: "Discard and reload" }),
    );
    // Nothing is copied from the old state: the draft and the question are still there.
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
    expect(screen.getByText(/changed somewhere else/)).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Save my version" }),
    ).toBeDisabled();

    held.open();
    await waitFor(() =>
      expect(screen.getByLabelText("Credit line")).toHaveValue("Theirs"),
    );
    expect(
      screen.queryByText(/changed somewhere else/),
    ).not.toBeInTheDocument();

    // The draft now belongs to the revision that was loaded: the next save goes through.
    await user.type(screen.getByLabelText("Credit line"), "!");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(9));
    expect(
      screen.queryByText(/changed somewhere else/),
    ).not.toBeInTheDocument();
  });

  it("keeps the draft and says so when what is saved now cannot be loaded", async () => {
    let fail = false;
    server.use(
      http.get("/api/translation/state", () =>
        fail
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json(state),
      ),
      http.patch("/api/translation/engines/:id", () => {
        fail = true;
        return HttpResponse.json(
          {
            error: "stale_revision",
            message: "The engine settings changed since they were loaded.",
            revision: 9,
          },
          { status: 409 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Mine");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await screen.findByText(/changed somewhere else/);
    await user.click(
      screen.getByRole("button", { name: "Discard and reload" }),
    );
    expect(
      await screen.findByText(
        "What is saved now could not be loaded, so nothing was changed. Try again.",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
  });

  it("Save my version clicked early carries the revision that was just loaded", async () => {
    let revision = 3;
    const held = gate();
    let holding = false;
    const sent: Array<{ expected_revision?: number }> = [];
    server.use(
      http.get("/api/translation/state", async () => {
        if (holding) {
          await held.opened;
        }
        return HttpResponse.json({
          ...state,
          instances: [second, engine({ revision })],
        });
      }),
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { expected_revision?: number };
        sent.push(body);
        if (body.expected_revision !== revision) {
          holding = true;
          return HttpResponse.json(
            {
              error: "stale_revision",
              message: "The engine settings changed since they were loaded.",
              revision,
            },
            { status: 409 },
          );
        }
        revision += 1;
        return HttpResponse.json({ engine: engine({ revision }), notices: [] });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    revision = 7;
    await user.type(screen.getByLabelText("Credit line"), "!");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await screen.findByText(/changed somewhere else/);
    await user.click(screen.getByRole("button", { name: "Save my version" }));
    // Nothing is sent with the old revision while the reload is open.
    expect(sent.map((body) => body.expected_revision)).toEqual([3]);
    held.open();
    await waitFor(() =>
      expect(sent.map((body) => body.expected_revision)).toEqual([3, 7]),
    );
  });
});

describe("Translation page, reordering", () => {
  it("names the row that was moved up, and keeps the focus on that row", async () => {
    server.use(
      http.put(
        "/api/translation/engines/:id/connections/order",
        async ({ request }) => {
          const body = (await request.json()) as { order: number[] };
          record("put", "order", body);
          // The server keeps the order, so the list stays as it was arranged.
          state = {
            ...state,
            instances: [
              second,
              engine({
                connections: body.order.map((id) => connection(id)),
              }),
            ],
          };
          return HttpResponse.json({ connections: [] });
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", {
        name: "Move OpenAI compatible, model-2 up",
      }),
    );
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "order", body: { order: [2, 1] } },
      ]),
    );
    expect(
      await screen.findByText(
        "OpenAI compatible, model-2 moved to position 1 of 2.",
      ),
    ).toBeInTheDocument();
    // Its Move up button is now disabled, at the top. The focus went to the row's other button.
    expect(
      screen.getByRole("button", {
        name: "Move OpenAI compatible, model-2 down",
      }),
    ).toHaveFocus();
  });

  it("stays usable while an order is being saved, and saves the last order asked for", async () => {
    const held = gate();
    let first = true;
    server.use(
      http.put(
        "/api/translation/engines/:id/connections/order",
        async ({ request }) => {
          const body = (await request.json()) as { order: number[] };
          record("put", "order", body);
          if (first) {
            first = false;
            await held.opened;
          }
          state = {
            ...state,
            instances: [
              second,
              engine({
                connections: body.order.map((id) => connection(id)),
              }),
            ],
          };
          return HttpResponse.json({ connections: [] });
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", {
        name: "Move OpenAI compatible, model-1 down",
      }),
    );
    await waitFor(() => expect(calls).toHaveLength(1));
    // The list shows the new order at once, and nothing in it is switched off while the save is open.
    expect(editNames()).toEqual([
      "Edit OpenAI compatible, model-2",
      "Edit OpenAI compatible, model-1",
    ]);
    const up = screen.getByRole("button", {
      name: "Move OpenAI compatible, model-1 up",
    });
    expect(up).toBeEnabled();
    expect(up).toHaveFocus();
    expect(
      screen.getByRole("button", { name: "Drag OpenAI compatible, model-2" }),
    ).toBeEnabled();
    // A second move made meanwhile is not lost and not sent on top of the first.
    await user.click(up);
    expect(calls).toHaveLength(1);
    expect(editNames()).toEqual([
      "Edit OpenAI compatible, model-1",
      "Edit OpenAI compatible, model-2",
    ]);
    held.open();
    await waitFor(() =>
      expect(calls.map((call) => call.body)).toEqual([
        { order: [2, 1] },
        { order: [1, 2] },
      ]),
    );
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(editNames()).toEqual([
      "Edit OpenAI compatible, model-1",
      "Edit OpenAI compatible, model-2",
    ]);
  });
});

describe("Translation page, switches, tabs and install", () => {
  it("sends one write when the switch is pressed twice", async () => {
    const held = gate();
    server.use(
      http.patch(
        "/api/translation/connections/:id",
        async ({ request, params }) => {
          record(
            "patch",
            `connection ${String(params.id)}`,
            await request.json(),
          );
          await held.opened;
          return HttpResponse.json({
            connection: connection(Number(params.id), { enabled: false }),
          });
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    const toggle = screen.getByLabelText("Use OpenAI compatible, model-1");
    await user.click(toggle);
    await waitFor(() => expect(calls).toHaveLength(1));
    // It shows where it is going, says it is busy, and keeps the focus.
    expect(toggle).not.toBeChecked();
    expect(toggle).toHaveAttribute("aria-busy", "true");
    expect(toggle).toBeEnabled();
    await user.click(toggle);
    await user.click(toggle);
    expect(calls).toEqual([
      {
        method: "patch",
        path: "connection 1",
        body: { enabled: false, expected_revision: 1 },
      },
    ]);
    held.open();
    await waitFor(() => expect(toggle).toHaveAttribute("aria-busy", "false"));
    expect(calls).toHaveLength(1);
  });

  it("keeps unsaved drafts on every tab while moving between them", async () => {
    const user = userEvent.setup();
    await showPage();
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Half written");
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const score = await screen.findByLabelText(
      "Score of a translated subtitle",
    );
    await user.clear(score);
    await user.type(score, "65");
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    await screen.findByText("Nothing has been translated yet");
    await user.click(screen.getByRole("tab", { name: "Engine" }));
    expect(screen.getByLabelText("Credit line")).toHaveValue("Half written");
    expect(screen.getByLabelText("Credit line")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    expect(screen.getByLabelText("Score of a translated subtitle")).toHaveValue(
      "65",
    );
    expect(screen.getByRole("button", { name: "Save defaults" })).toBeEnabled();
  });

  it("keeps a back end draft while another engine's set-up is looked at", async () => {
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    const url = screen.getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "http://half:1/v1");
    await user.click(
      screen.getByRole("button", { name: "Set up Google Translate" }),
    );
    expect(await screen.findByText("Nothing to set up")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Set up AI Subtitle Translator" }),
    );
    expect(screen.getByLabelText("Base URL")).toHaveValue("http://half:1/v1");
  });

  it("does not show one install's failure the next time the question is asked", async () => {
    state = {
      ...state,
      available_engines: [
        {
          plugin_id: "lingarr",
          name: "Lingarr",
          version: "1.0.0",
          source: "official",
        },
        {
          plugin_id: "other_engine",
          name: "Other Engine",
          version: "2.0.0",
          source: "official",
        },
      ],
    };
    server.use(
      http.post(
        "/api/provider-hub/installations",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Install Lingarr" }));
    const first = await screen.findByRole("dialog", {
      name: "Install Lingarr?",
    });
    await user.click(within(first).getByRole("button", { name: "Install" }));
    expect(
      await within(first).findByText(/The install did not finish/),
    ).toBeVisible();
    await user.click(within(first).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Install Lingarr?" }),
      ).not.toBeInTheDocument(),
    );
    // Another engine, then the same one again: neither starts with the old failure.
    await user.click(
      screen.getByRole("button", { name: "Install Other Engine" }),
    );
    const other = await screen.findByRole("dialog", {
      name: "Install Other Engine?",
    });
    expect(
      within(other).queryByText(/The install did not finish/),
    ).not.toBeInTheDocument();
    await user.click(within(other).getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Install Lingarr" }));
    const again = await screen.findByRole("dialog", {
      name: "Install Lingarr?",
    });
    expect(
      within(again).queryByText(/The install did not finish/),
    ).not.toBeInTheDocument();
  });

  it("lists the waiting jobs on a server that reports them", async () => {
    state = { ...state, capabilities: ["queue"] };
    server.use(
      http.get("/api/translation/queue", () =>
        HttpResponse.json({
          data: [
            {
              job_id: "a",
              media_type: "movie",
              media_id: 1,
              media_title: "Arrival",
              source_language: "en",
              target_language: "hu",
              engine_instance_id: 1,
              status: "queued",
              queued_at: null,
            },
            {
              job_id: "b",
              media_type: "movie",
              media_id: 2,
              media_title: "Flow",
              source_language: "en",
              target_language: "de",
              engine_instance_id: 1,
              status: "awaiting_retry",
              queued_at: null,
            },
          ],
          total: 5,
        }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const waiting = await screen.findByRole("list", { name: "Waiting" });
    expect(within(waiting).getByText("Arrival")).toBeVisible();
    expect(within(waiting).getByText("English to German")).toBeVisible();
    expect(within(waiting).getByText("Queued")).toBeVisible();
    expect(within(waiting).getByText("Waiting to retry")).toBeVisible();
    expect(screen.getByText("And 3 more.")).toBeVisible();
  });
});
