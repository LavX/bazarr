/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { plainSummary } from "@/components/plugin-consent/fixtures";
import {
  forgetPasswordConfirmation,
  markPasswordConfirmed,
} from "@/modules/passwordConfirm";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type {
  TranslationHistoryJob,
  TranslationState,
} from "@/types/translation";
import {
  connection,
  engine,
  inputsHolding,
  job,
  lingarr,
  second,
  SECRET,
} from "./fixtures";
import TranslationView from ".";

// The page's longer flows: what stays on screen while a request is on its way,
// and what a failure in the middle leaves behind.

let state: TranslationState;
let calls: Array<{ method: string; path: string; body: unknown }>;

function record(method: string, path: string, body: unknown) {
  calls.push({ method, path, body });
}

/** What the server says installing an engine adds: nothing beyond the engine itself. */
const consentHandler = () =>
  http.get("/api/plugins/catalog/consent", ({ request }) => {
    const id = new URL(request.url).searchParams.get("id") ?? "";
    return HttpResponse.json({
      ...plainSummary,
      plugin: { ...plainSummary.plugin, id },
    });
  });

/** Agrees to what the dialog shows, once it has finished loading it. */
const agree = async (
  user: ReturnType<typeof userEvent.setup>,
  ask: HTMLElement,
) => {
  const button = await within(ask).findByRole("button", { name: "Install" });
  await waitFor(() => expect(button).toBeEnabled());
  await user.click(button);
  return button;
};

const FAILED_INSTALL =
  "The install did not finish. Check the Activity tab of the Plugin Hub for the reason.";

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
  forgetPasswordConfirmation();
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
    // As the server answers: a model list only for a back-end type the engine has, a refusal for any other.
    http.post(
      "/api/translation/engines/:id/options",
      async ({ request, params }) => {
        const { backend_type } = (await request.json()) as {
          backend_type: string;
        };
        const owner = state.instances.find(
          (item) => item.id === Number(params.id),
        );
        if (!owner?.schema?.backend_types[backend_type]) {
          return HttpResponse.json(
            {
              error: "bad_request",
              message: `${owner?.name ?? "The engine"} has no back end of type ${backend_type}`,
            },
            { status: 400 },
          );
        }
        return HttpResponse.json({
          available: false,
          items: [],
          reason: "none",
        });
      },
    ),
  );
});

async function showPage() {
  customRender(<TranslationView />);
  return screen.findByRole("radiogroup", { name: "Engine" });
}

/** The alert that holds this sentence, to reach the button beside it. */
const alertSaying = (text: string) =>
  screen
    .getAllByRole("alert")
    .find((alert) => within(alert).queryByText(text) !== null)!;

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
    // The state was loaded again once the back end existed, so the rest of the page knows about it.
    // That load reports a back end, and it did not take the panel away.
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(loads).toBeGreaterThan(before);
    expect(
      screen.getByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    // The key is not asked for again, and is not kept in the page.
    expect(screen.getByLabelText("OpenRouter key")).toBeDisabled();
    expect(screen.getByLabelText("OpenRouter key")).toHaveValue("");
    expect(inputsHolding(SECRET)).toHaveLength(0);

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
    // The form is locked meanwhile: what is saved is what it held when the answer was given.
    expect(screen.getByLabelText("Credit line")).toBeDisabled();
    await user.type(screen.getByLabelText("Credit line"), " and more");
    expect(screen.getByLabelText("Credit line")).toHaveValue(
      "Translated by Bazarr+!",
    );
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
      consentHandler(),
      http.post(
        "/api/plugins/installations",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Install Lingarr" }));
    const first = await screen.findByRole("dialog", {
      name: "Install Lingarr?",
    });
    await agree(user, first);
    // The server gave no reason: the dialog's own sentence, whole, and nothing from the transport.
    expect(await within(first).findByText(FAILED_INSTALL)).toBeVisible();
    expect(within(first).queryByText(/status code/)).not.toBeInTheDocument();
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

describe("Translation page, choices made faster than they are saved", () => {
  // A server that keeps the pointers it is sent, and answers the first write only when let through.
  function pointerServer() {
    const held = gate();
    let first = true;
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        const body = (await request.json()) as {
          engine?: number | null;
          fallback_engine?: number | null;
        };
        record("put", "pointers", body);
        if (first) {
          first = false;
          await held.opened;
        }
        state = { ...state, ...body };
        return HttpResponse.json({
          engine: state.engine,
          fallback_engine: state.fallback_engine,
          notices: [],
        });
      }),
    );
    return held;
  }

  it("writes one choice at a time, and None followed by an engine ends on the engine", async () => {
    const held = pointerServer();
    const user = userEvent.setup();
    const group = await showPage();
    const none = within(group).getByRole("radio", { name: "None" });
    const google = within(group).getByRole("radio", {
      name: "Google Translate",
    });
    await user.click(none);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(none).toBeChecked();
    // Chosen while the first write is still open: shown at once, sent only after it.
    await user.click(google);
    expect(google).toBeChecked();
    expect(calls).toHaveLength(1);
    held.open();
    await waitFor(() =>
      expect(calls.map((call) => call.body)).toEqual([
        { engine: null },
        { engine: 2 },
      ]),
    );
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(google).toBeChecked();
    expect(state.engine).toBe(2);
  });

  it("sends only the last of several choices made during one write", async () => {
    const held = pointerServer();
    const user = userEvent.setup();
    const group = await showPage();
    const none = within(group).getByRole("radio", { name: "None" });
    const google = within(group).getByRole("radio", {
      name: "Google Translate",
    });
    const official = within(group).getByRole("radio", {
      name: /AI Subtitle Translator/,
    });
    await user.click(google);
    await waitFor(() => expect(calls).toHaveLength(1));
    await user.click(none);
    await user.click(official);
    await user.click(none);
    expect(calls).toHaveLength(1);
    held.open();
    await waitFor(() =>
      expect(calls.map((call) => call.body)).toEqual([
        { engine: 2 },
        { engine: null },
      ]),
    );
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(none).toBeChecked();
    expect(state.engine).toBeNull();
  });
});

describe("Translation page, a form and the controls around it", () => {
  it("keeps the row's switch off limits while that row's form is open", async () => {
    const user = userEvent.setup();
    await showPage();
    const toggle = screen.getByLabelText("Use OpenAI compatible, model-1");
    expect(toggle).toBeEnabled();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    expect(toggle).toBeDisabled();
    // The other row's switch is not affected.
    expect(
      screen.getByLabelText("Use OpenAI compatible, model-2"),
    ).toBeEnabled();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    expect(toggle).toBeEnabled();
  });

  it("does not let the consent question be closed while its answer is on its way", async () => {
    const held = gate();
    let attempt = 0;
    server.use(
      http.patch("/api/translation/connections/:id", async ({ request }) => {
        attempt += 1;
        record("patch", "connection", await request.json());
        if (attempt === 1) {
          return HttpResponse.json(
            {
              error: "consent_required",
              message: "These addresses are new.",
              destinations: ["http://new:1/v1"],
            },
            { status: 409 },
          );
        }
        await held.opened;
        return HttpResponse.json({ connection: connection(1) });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    const url = screen.getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "http://new:1/v1");
    await user.click(screen.getByRole("button", { name: "Save back end" }));
    const ask = await screen.findByRole("dialog", {
      name: "Send subtitles to a new address?",
    });
    await user.click(
      within(ask).getByRole("button", { name: "Send to this address" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    // The answer is still open. Escape does not hide the question, and there is no Cancel to press.
    await user.keyboard("{Escape}");
    expect(
      screen.getByRole("dialog", { name: "Send subtitles to a new address?" }),
    ).toBeVisible();
    expect(within(ask).getByRole("button", { name: "Cancel" })).toBeDisabled();
    held.open();
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", {
          name: "Send subtitles to a new address?",
        }),
      ).not.toBeInTheDocument(),
    );
  });

  it("keeps Activity as it was left when the page was opened on that tab", async () => {
    window.history.replaceState({}, "", "/?tab=activity");
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await screen.findByText("Nothing has been translated yet");
    await user.click(screen.getByRole("radio", { name: "Failed" }));
    await user.click(screen.getByRole("tab", { name: "Engine" }));
    await screen.findByRole("radiogroup", { name: "Engine" });
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    expect(screen.getByRole("radio", { name: "Failed" })).toBeChecked();
  });
});

describe("Translation page, first run with what is really installed", () => {
  const firstRun = (
    patch: Partial<TranslationState> = {},
  ): TranslationState => ({
    enabled: true,
    engine: null,
    fallback_engine: null,
    instances: [engine({ configured: false, connections: [] })],
    ...patch,
  });

  it("offers an installed engine for use only when it can take a job", async () => {
    const broken = {
      ...second,
      id: 3,
      plugin_id: "lingarr",
      name: "Lingarr",
      plugin: { name: "Lingarr", version: "1.0.0", active: false },
    };
    const unfinished = {
      ...second,
      id: 4,
      plugin_id: "other_engine",
      name: "Other Engine",
      configured: false,
      plugin: { name: "Other Engine", version: "1.0.0", active: true },
    };
    state = firstRun({
      instances: [
        engine({ configured: false, connections: [] }),
        second,
        broken,
        unfinished,
      ],
    });
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: 2,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await screen.findByRole("heading", {
      name: "Translate the subtitles you cannot find",
    });
    // Not running: its state and the way out, no Use.
    expect(screen.getByText("Plugin not running")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Use Lingarr" }),
    ).not.toBeInTheDocument();
    // Running but not set up: it is sent to its set-up, not offered as ready.
    expect(screen.getByText("Not set up")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Use Other Engine" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Set up Other Engine" }),
    ).toBeVisible();
    // Ready: offered, and picking it selects it.
    await user.click(
      screen.getByRole("button", { name: "Use Google Translate" }),
    );
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "pointers", body: { engine: 2 } },
      ]),
    );
  });

  it("says nothing about the catalog when the server does not list what can be installed", async () => {
    state = firstRun({ instances: [] });
    customRender(<TranslationView />);
    await screen.findByRole("heading", {
      name: "Translate the subtitles you cannot find",
    });
    expect(
      screen.getByText(
        "It is not installed. Install it in the Plugin Hub, then come back.",
      ),
    ).toBeVisible();
    expect(screen.queryByText(/plugin catalog/)).not.toBeInTheDocument();
    expect(screen.queryByText("Google Translate")).not.toBeInTheDocument();
  });

  it("says the catalog offers none when the server lists an empty one", async () => {
    state = firstRun({ instances: [], available_engines: [] });
    customRender(<TranslationView />);
    await screen.findByRole("heading", {
      name: "Translate the subtitles you cannot find",
    });
    expect(
      screen.getByText(/the plugin catalog does not list it right now/),
    ).toBeVisible();
    expect(
      screen.getByText(/It is not in the plugin catalog yet/),
    ).toBeVisible();
  });

  it("shows the back end it saved once the start failed half way, so it is not added twice by hand", async () => {
    state = firstRun();
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          const created = connection(3, {
            backend_type: "openrouter",
            model: "vendor/starter-model",
          });
          state = {
            ...state,
            instances: [engine({ connections: [created] })],
          };
          return HttpResponse.json({ connection: created }, { status: 201 });
        },
      ),
      http.put("/api/translation/pointers", () =>
        HttpResponse.json(
          { error: "bad_request", message: "The engine is not ready." },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    await screen.findByText(
      /Your key is saved, but the engine could not be switched on/,
    );
    await user.click(screen.getByRole("button", { name: "Set it up by hand" }));
    await screen.findByRole("radiogroup", { name: "Engine" });
    // The page already has the back end: nothing says one is still needed.
    expect(
      screen.getByRole("button", {
        name: "Edit OpenRouter, vendor/starter-model",
      }),
    ).toBeVisible();
    expect(screen.queryByText("No back ends yet")).not.toBeInTheDocument();
    expect(screen.queryByText("Needs a back end")).not.toBeInTheDocument();
    expect(calls.map((call) => call.path)).toEqual(["connection"]);
  });
});

describe("Translation page, turning translation off on the full page", () => {
  it("stays on the full page when None is picked and nothing has a back end", async () => {
    // Only an engine that needs no set-up, selected: after None the state looks like a first run.
    state = { ...state, engine: 2, instances: [second] };
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        const body = (await request.json()) as { engine: number | null };
        record("put", "pointers", body);
        state = { ...state, engine: body.engine };
        return HttpResponse.json({
          engine: body.engine,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    await user.click(within(group).getByRole("radio", { name: "None" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(screen.getByRole("radiogroup", { name: "Engine" })).toBeVisible();
    expect(within(group).getByRole("radio", { name: "None" })).toBeChecked();
    expect(
      screen.queryByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).not.toBeInTheDocument();
  });
});

describe("Translation page, an engine Bazarr+ runs itself", () => {
  it("lists it as a choice with no plugin trouble, and selects it like any engine", async () => {
    state = { ...state, instances: [second, engine(), lingarr()] };
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: 5,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    const radio = within(group).getByRole("radio", { name: /Lingarr/ });
    expect(radio).toHaveAccessibleName(/Legacy/);
    // It has no plugin, and that is not reported as a plugin that stopped.
    expect(screen.queryByText("Plugin not running")).not.toBeInTheDocument();
    // What it does, and what "Legacy" means, are both said in words on the card.
    expect(
      screen.getByText(/Sends each job to your own Lingarr service\./),
    ).toBeVisible();
    expect(
      screen.getByText(/Kept from earlier versions: it is not a plugin/),
    ).toBeVisible();
    expect(radio).toHaveAccessibleDescription(
      /Kept from earlier versions: it is not a plugin/,
    );
    await user.click(radio);
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "pointers", body: { engine: 5 } },
      ]),
    );
  });

  it("draws its URL and token from its schema, asks before a new address, and keeps the token write-only", async () => {
    state = {
      ...state,
      engine: 5,
      instances: [
        engine(),
        lingarr({ configured: false, values: {}, secrets: {} }),
      ],
    };
    let attempt = 0;
    server.use(
      http.patch(
        "/api/translation/engines/:id",
        async ({ request, params }) => {
          const body = (await request.json()) as {
            accept_destinations?: string[];
          };
          record("patch", "engine " + String(params.id), body);
          attempt += 1;
          if (attempt === 1) {
            return HttpResponse.json(
              {
                error: "consent_required",
                message: "These addresses are new.",
                destinations: ["http://lingarr.lan:9876"],
              },
              { status: 409 },
            );
          }
          const saved = lingarr({
            revision: 2,
            values: { url: "http://lingarr.lan:9876" },
            secrets: { token: { configured: true, last4: "6789" } },
          });
          state = { ...state, instances: [engine(), saved] };
          return HttpResponse.json({ engine: saved, notices: [] });
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    // Selected and not set up yet: the page says so, in the band and on the card.
    const band = screen.getByRole("group", { name: "Translation status" });
    expect(within(band).getByText("Not set up")).toBeVisible();
    await user.type(
      screen.getByRole("textbox", { name: /Lingarr URL/ }),
      "http://lingarr.lan:9876",
    );
    await user.type(screen.getByLabelText("API token"), SECRET);
    // Two engines have a Save button; the one on screen is Lingarr's.
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Send subtitles to a new address?",
    });
    await waitFor(() =>
      expect(within(ask).getByText("http://lingarr.lan:9876")).toBeVisible(),
    );
    await user.click(
      within(ask).getByRole("button", { name: "Send to this address" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1]).toEqual({
      method: "patch",
      path: "engine 5",
      body: {
        expected_revision: 1,
        values: { url: "http://lingarr.lan:9876", token: SECRET },
        clear_secrets: [],
        accept_destinations: ["http://lingarr.lan:9876"],
      },
    });
    // Saved: ready, and the token is only said to be there.
    expect(await within(band).findByText("Ready")).toBeVisible();
    expect(
      await screen.findByPlaceholderText(
        "Saved, ending 6789. Leave empty to keep it.",
      ),
    ).toHaveValue("");
    expect(inputsHolding(SECRET)).toHaveLength(0);
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("is reached from the first-run panel, which opens the full page on its set-up", async () => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [
        engine({ configured: false, connections: [] }),
        lingarr({ configured: false, values: {}, secrets: {} }),
      ],
    };
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await screen.findByRole("heading", {
      name: "Translate the subtitles you cannot find",
    });
    // Not set up, so it is not offered for use yet.
    expect(
      screen.queryByRole("button", { name: "Use Lingarr" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Set up Lingarr" }));
    const group = await screen.findByRole("radiogroup", { name: "Engine" });
    expect(within(group).getByRole("radio", { name: "None" })).toBeChecked();
    // The full page opened on Lingarr's own form.
    expect(screen.getByRole("textbox", { name: /Lingarr URL/ })).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Lingarr", level: 2 }),
    ).toBeVisible();
  });

  it("is offered for use on first run once its URL is set", async () => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [engine({ configured: false, connections: [] }), lingarr()],
    };
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: 5,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.click(
      await screen.findByRole("button", { name: "Use Lingarr" }),
    );
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "pointers", body: { engine: 5 } },
      ]),
    );
  });
});

describe("Translation page, one thing at a time", () => {
  it("lets nothing else be chosen on the first-run panel while a start is on its way", async () => {
    const held = gate();
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [
        engine({ configured: false, connections: [] }),
        second,
        lingarr({ configured: false, values: {}, secrets: {} }),
      ],
      available_engines: [
        {
          plugin_id: "other_engine",
          name: "Other Engine",
          version: "1.0.0",
          source: "official",
          free: true,
        },
      ],
    };
    server.use(
      http.post("/api/translation/engines/:id/connections", async () => {
        record("post", "connection", null);
        await held.opened;
        return HttpResponse.json(
          { error: "invalid", message: "The values are not valid." },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    const others = () => [
      screen.getByRole("button", { name: "Use Google Translate" }),
      screen.getByRole("button", { name: "Set up Lingarr" }),
      screen.getByRole("button", { name: "Install Other Engine" }),
      screen.getByRole("button", { name: "Set it up by hand" }),
    ];
    others().forEach((button) => expect(button).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    // The start is open: every other way out of the panel waits for it.
    others().forEach((button) => expect(button).toBeDisabled());
    await user.click(screen.getByRole("button", { name: "Set it up by hand" }));
    expect(
      screen.getByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).toBeVisible();
    held.open();
    await screen.findByText("The values are not valid.");
    others().forEach((button) => expect(button).toBeEnabled());
  });

  it("locks the key form while another engine is being picked", async () => {
    const held = gate();
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [engine({ configured: false, connections: [] }), second],
    };
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        await held.opened;
        return HttpResponse.json(
          { error: "bad_request", message: "Not now." },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.click(
      await screen.findByRole("button", { name: "Use Google Translate" }),
    );
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(
      screen.getByRole("button", { name: "Start translating" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("OpenRouter key")).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Set it up by hand" }),
    ).toBeDisabled();
    held.open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Start translating" }),
      ).toBeEnabled(),
    );
  });

  it("does not let a back end be removed while its save is on its way, or saved while it is being removed", async () => {
    const saving = gate();
    const removing = gate();
    server.use(
      http.patch("/api/translation/connections/:id", async () => {
        record("patch", "connection", null);
        await saving.opened;
        return HttpResponse.json(
          { error: "invalid", message: "The values are not valid." },
          { status: 400 },
        );
      }),
      http.delete("/api/translation/connections/:id", async () => {
        record("delete", "connection", null);
        await removing.opened;
        return new HttpResponse(null, { status: 500 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    await user.click(screen.getByRole("button", { name: "Save back end" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    // Saving: the removal cannot be started.
    expect(
      screen.getByRole("button", { name: "Remove back end" }),
    ).toBeDisabled();
    saving.open();
    await screen.findByText("The values are not valid.");
    await user.click(screen.getByRole("button", { name: "Remove back end" }));
    await user.click(screen.getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    // Removing: no save, no way out of the form, no typing.
    expect(
      screen.getByRole("button", { name: "Save back end" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Cancel" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Keep" })).toBeDisabled();
    expect(screen.getByLabelText("Base URL")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Save back end" }));
    expect(calls).toHaveLength(2);
    removing.open();
    // The removal failed: the reason is shown and the form is usable again.
    expect(
      await screen.findByText("The back end could not be removed. Try again."),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Save back end" })).toBeEnabled();
    expect(screen.getByLabelText("Base URL")).toBeEnabled();
  });

  it("does not let a back end be removed while a conflict answer loads what is saved", async () => {
    const held = gate();
    let holding = false;
    server.use(
      http.get("/api/translation/state", async () => {
        if (holding) {
          await held.opened;
        }
        return HttpResponse.json(state);
      }),
      http.patch("/api/translation/connections/:id", () => {
        holding = true;
        return HttpResponse.json(
          {
            error: "stale_revision",
            message: "The back end changed since it was loaded.",
            revision: 2,
          },
          { status: 409 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    await user.click(screen.getByRole("button", { name: "Save back end" }));
    await screen.findByText(/changed somewhere else/);
    await user.click(
      screen.getByRole("button", { name: "Discard and reload" }),
    );
    expect(
      screen.getByRole("button", { name: "Remove back end" }),
    ).toBeDisabled();
    held.open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Remove back end" }),
      ).toBeEnabled(),
    );
  });

  it("drops the waiting choice, says why, and goes back to what is saved when a pointer write is refused", async () => {
    const held = gate();
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        await held.opened;
        return HttpResponse.json(
          { error: "bad_request", message: "That engine cannot be selected." },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    const official = within(group).getByRole("radio", {
      name: /AI Subtitle Translator/,
    });
    const google = within(group).getByRole("radio", {
      name: "Google Translate",
    });
    const none = within(group).getByRole("radio", { name: "None" });
    expect(official).toBeChecked();
    await user.click(google);
    await waitFor(() => expect(calls).toHaveLength(1));
    // A second choice waits behind the first.
    await user.click(none);
    expect(none).toBeChecked();
    held.open();
    expect(
      await screen.findByText("That engine cannot be selected."),
    ).toBeVisible();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    // The choice that waited was made on top of the refused one: it is not sent.
    expect(calls.map((call) => call.body)).toEqual([{ engine: 2 }]);
    // The list shows what is saved again.
    await waitFor(() => expect(official).toBeChecked());
    expect(google).not.toBeChecked();
    expect(none).not.toBeChecked();
  });
});

describe("Translation page, what a test result and a disabled switch say", () => {
  it("forgets a test result once the back end has changed, and says the result aloud", async () => {
    state = { ...state, capabilities: ["test"] };
    server.use(
      http.post("/api/translation/connections/:id/test", () =>
        HttpResponse.json({ ok: true, message: "", latency_ms: 820 }),
      ),
      http.patch("/api/translation/connections/:id", ({ params }) => {
        const id = Number(params.id);
        // The switch moved the back end on to its next revision.
        state = {
          ...state,
          instances: [
            second,
            engine({
              connections: [
                connection(1),
                connection(2, { enabled: false, revision: 2 }),
              ],
            }),
          ],
        };
        return HttpResponse.json({
          connection: connection(id, { enabled: false, revision: 2 }),
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Test OpenAI compatible, model-2" }),
    );
    expect(
      await screen.findByText("It works. Answered in 820 ms."),
    ).toBeVisible();
    // The same answer goes through the live region, named after its row.
    const said = screen
      .getAllByRole("status")
      .find(
        (region) =>
          region.textContent ===
          "OpenAI compatible, model-2: It works. Answered in 820 ms.",
      );
    expect(said).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByLabelText("Use OpenAI compatible, model-2"));
    // It is a different back end now: what was tested is not what is saved.
    await waitFor(() =>
      expect(
        screen.queryByText("It works. Answered in 820 ms."),
      ).not.toBeInTheDocument(),
    );
  });

  it("says why a row's switch cannot be used while its form is open", async () => {
    const user = userEvent.setup();
    await showPage();
    const toggle = screen.getByLabelText("Use OpenAI compatible, model-1");
    expect(toggle).not.toHaveAccessibleDescription();
    await user.click(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    );
    expect(toggle).toHaveAccessibleDescription(
      "Save or close the form to switch this back end on or off.",
    );
  });
});

describe("Translation page, Activity when a list cannot be loaded", () => {
  const rows = (status: string | null): TranslationHistoryJob[] =>
    [
      job(1, { status: "running", finished_at: null }),
      job(2, { media_title: "Dune: Part Two" }),
    ].filter((item) => !status || item.status === status);

  it("says the running jobs could not be loaded, and loads them on Try again", async () => {
    let fail = true;
    server.use(
      http.get("/api/translation/history", ({ request }) => {
        const status = new URL(request.url).searchParams.get("status");
        if (status === "running" && fail) {
          return new HttpResponse(null, { status: 500 });
        }
        const data = rows(status);
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const problem = await screen.findByText(
      "The running jobs could not be loaded.",
    );
    expect(problem).toBeVisible();
    // The history, which did load, is still there.
    expect(await screen.findByText("Dune: Part Two")).toBeVisible();
    fail = false;
    await user.click(
      within(alertSaying(problem.textContent ?? "")).getByRole("button", {
        name: "Try again",
      }),
    );
    const now = await screen.findByRole("list", { name: "Translating now" });
    expect(within(now).getByText("Film 1")).toBeVisible();
    expect(
      screen.queryByText("The running jobs could not be loaded."),
    ).not.toBeInTheDocument();
  });

  it("keeps the rows it has, marked as possibly out of date, when an update fails", async () => {
    let fail = false;
    server.use(
      http.get("/api/translation/history", ({ request }) => {
        if (fail) {
          return new HttpResponse(null, { status: 500 });
        }
        const data = rows(new URL(request.url).searchParams.get("status"));
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const now = await screen.findByRole("list", { name: "Translating now" });
    expect(now).toHaveAttribute("data-stale", "false");
    fail = true;
    await queryClient.invalidateQueries();
    expect(
      await screen.findByText(
        "The running jobs could not be updated. What is listed may be out of date.",
      ),
    ).toBeVisible();
    expect(
      await screen.findByText(
        "The history could not be updated. What is listed may be out of date.",
      ),
    ).toBeVisible();
    // Nothing was thrown away: the rows are still listed, and marked.
    expect(within(now).getByText("Film 1")).toBeVisible();
    expect(now).toHaveAttribute("data-stale", "true");
    expect(screen.getByRole("list", { name: "History" })).toHaveAttribute(
      "data-stale",
      "true",
    );
  });

  it("says the waiting jobs could not be loaded, and loads them on Try again", async () => {
    state = { ...state, capabilities: ["queue"] };
    let fail = true;
    server.use(
      http.get("/api/translation/queue", () =>
        fail
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({
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
              ],
              total: 1,
            }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const problem = await screen.findByText(
      "The waiting jobs could not be loaded.",
    );
    fail = false;
    await user.click(
      within(alertSaying(problem.textContent ?? "")).getByRole("button", {
        name: "Try again",
      }),
    );
    const waiting = await screen.findByRole("list", { name: "Waiting" });
    expect(within(waiting).getByText("Arrival")).toBeVisible();
    expect(
      screen.queryByText("The waiting jobs could not be loaded."),
    ).not.toBeInTheDocument();
  });
});

describe("Translation page, first run with the official engine installed but not running", () => {
  it("says what the engine reports, not that it is not installed", async () => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [
        engine({
          configured: false,
          connections: [],
          plugin: {
            name: "AI Subtitle Translator",
            version: "2.2.0",
            active: false,
          },
        }),
      ],
      available_engines: [],
    };
    customRender(<TranslationView />);
    await screen.findByRole("heading", {
      name: "Translate the subtitles you cannot find",
    });
    expect(screen.getByText("Plugin not running")).toBeVisible();
    expect(
      screen.getByText(
        /Its plugin is not installed or not running\. Repair or reinstall it in the Plugin Hub\./,
      ),
    ).toBeVisible();
    expect(screen.queryByText(/^It is not installed/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Start translating" }),
    ).not.toBeInTheDocument();
  });
});

describe("Translation page, a form that is in the middle of something", () => {
  it("cannot be closed or replaced by another form while its save is on its way", async () => {
    const held = gate();
    server.use(
      http.patch("/api/translation/connections/:id", async () => {
        record("patch", "connection", null);
        await held.opened;
        return HttpResponse.json(
          {
            error: "invalid",
            message: "The values are not valid.",
            field_errors: { base_url: "is not an address" },
          },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const own = screen.getByRole("button", {
      name: "Edit OpenAI compatible, model-1",
    });
    const other = screen.getByRole("button", {
      name: "Edit OpenAI compatible, model-2",
    });
    const add = screen.getByRole("button", { name: "Add a back end" });
    await user.click(own);
    const url = screen.getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "nonsense");
    await user.click(screen.getByRole("button", { name: "Save back end" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    // The save is open: this form stays, and no other can take its place.
    expect(own).toBeDisabled();
    expect(other).toBeDisabled();
    expect(add).toBeDisabled();
    await user.click(other);
    await user.click(own);
    await user.click(add);
    expect(screen.getAllByLabelText("Base URL")).toHaveLength(1);
    expect(screen.getByLabelText("Base URL")).toHaveValue("nonsense");
    held.open();
    // The answer arrives in the form it was given in.
    expect(await screen.findByText("is not an address")).toBeVisible();
    expect(screen.getByLabelText("Base URL")).toHaveValue("nonsense");
    expect(own).toBeEnabled();
    expect(other).toBeEnabled();
    expect(add).toBeEnabled();
    // Now another form can be opened.
    await user.click(other);
    expect(screen.getByLabelText("Model")).toHaveValue("model-2");
  });

  it("holds the list's controls back while a new back end is being added", async () => {
    const held = gate();
    server.use(
      http.post("/api/translation/engines/:id/connections", async () => {
        record("post", "connection", null);
        await held.opened;
        return HttpResponse.json(
          { error: "invalid", message: "The values are not valid." },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Add a back end" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Custom", hidden: true }),
    );
    await user.type(await screen.findByLabelText("Base URL"), "http://x:1/v1");
    await user.click(screen.getByRole("button", { name: "Add back end" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    ).toBeDisabled();
    held.open();
    await screen.findByText("The values are not valid.");
    expect(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-1" }),
    ).toBeEnabled();
    expect(screen.getByLabelText("Base URL")).toHaveValue("http://x:1/v1");
  });
});

describe("Translation page, an engine picked while it waits as the fallback", () => {
  it("drops the waiting fallback, so the two are never sent as the same engine", async () => {
    const held = gate();
    let first = true;
    state = { ...state, engine: 2, fallback_engine: null };
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        const body = (await request.json()) as {
          engine?: number | null;
          fallback_engine?: number | null;
        };
        record("put", "pointers", body);
        if (first) {
          first = false;
          await held.opened;
        }
        state = { ...state, ...body };
        return HttpResponse.json({
          engine: state.engine,
          fallback_engine: state.fallback_engine,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    // A first write, held open, so that what follows has to wait.
    await user.click(within(group).getByRole("radio", { name: "None" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    // Fallback: the official engine. Then the official engine as the engine itself.
    const input = screen.getByRole("combobox", { name: "Fallback engine" });
    await user.click(input);
    const listbox = (
      await screen.findAllByRole("listbox", { hidden: true })
    ).find(
      (box) => box.getAttribute("id") === input.getAttribute("aria-controls"),
    )!;
    await user.click(
      within(listbox).getByRole("option", {
        name: "AI Subtitle Translator (Official)",
        hidden: true,
      }),
    );
    await user.click(
      within(group).getByRole("radio", { name: /AI Subtitle Translator/ }),
    );
    expect(calls).toHaveLength(1);
    held.open();
    await waitFor(() => expect(calls).toHaveLength(2));
    // One request, with the engine and without the fallback that named the same one.
    expect(calls[1].body).toEqual({ engine: 1 });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    expect(state.engine).toBe(1);
    expect(state.fallback_engine).toBeNull();
    expect(
      within(group).getByRole("radio", { name: /AI Subtitle Translator/ }),
    ).toBeChecked();
  });
});

describe("Translation page, moving on from a start that stopped half way", () => {
  it("leaves the first-run panel once another engine is picked there", async () => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [engine({ configured: false, connections: [] }), second],
    };
    let pointerFails = true;
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          const created = connection(3, { backend_type: "openrouter" });
          state = {
            ...state,
            instances: [engine({ connections: [created] }), second],
          };
          return HttpResponse.json({ connection: created }, { status: 201 });
        },
      ),
      http.put("/api/translation/pointers", async ({ request }) => {
        const body = (await request.json()) as { engine: number | null };
        record("put", "pointers", body);
        if (pointerFails) {
          return HttpResponse.json(
            { error: "bad_request", message: "The engine is not ready." },
            { status: 400 },
          );
        }
        state = { ...state, engine: body.engine };
        return HttpResponse.json({
          engine: body.engine,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    await screen.findByText(
      /Your key is saved, but the engine could not be switched on/,
    );
    pointerFails = false;
    await user.click(
      screen.getByRole("button", { name: "Use Google Translate" }),
    );
    // Picked: the panel is not held up by the start any more.
    const group = await screen.findByRole("radiogroup", { name: "Engine" });
    expect(
      within(group).getByRole("radio", { name: "Google Translate" }),
    ).toBeChecked();
    expect(
      screen.queryByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).not.toBeInTheDocument();
    expect(calls.at(-1)).toEqual({
      method: "put",
      path: "pointers",
      body: { engine: 2 },
    });
  });
});

describe("Translation page, a history that cannot be loaded", () => {
  it("says so with a way to ask again, and claims nothing about what was translated", async () => {
    let fail = true;
    server.use(
      http.get("/api/translation/history", ({ request }) => {
        const status = new URL(request.url).searchParams.get("status");
        if (fail && status !== "running") {
          return new HttpResponse(null, { status: 500 });
        }
        const data = status ? [] : [job(2, { media_title: "Dune: Part Two" })];
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const problem = await screen.findByText("The history could not be loaded.");
    expect(
      screen.queryByText("Nothing has been translated yet"),
    ).not.toBeInTheDocument();
    fail = false;
    await user.click(
      within(alertSaying(problem.textContent ?? "")).getByRole("button", {
        name: "Try again",
      }),
    );
    expect(await screen.findByText("Dune: Part Two")).toBeVisible();
    expect(
      screen.queryByText("The history could not be loaded."),
    ).not.toBeInTheDocument();
  });

  it("puts the failed update above an empty history it loaded earlier", async () => {
    let fail = false;
    server.use(
      http.get("/api/translation/history", () =>
        fail
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json({ data: [], total: 0 }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    await screen.findByText("Nothing has been translated yet");
    fail = true;
    await queryClient.invalidateQueries();
    const problem = await screen.findByText(
      "The history could not be updated. What is listed may be out of date.",
    );
    // The earlier answer is still shown, under the notice and its retry.
    const empty = screen.getByText("Nothing has been translated yet");
    expect(empty).toBeVisible();
    expect(
      problem.compareDocumentPosition(empty) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(
      within(alertSaying(problem.textContent ?? "")).getByRole("button", {
        name: "Try again",
      }),
    ).toBeVisible();
  });
});

describe("Translation page, the same test result twice", () => {
  it("empties the live region when a test starts, so the same answer is said again", async () => {
    state = { ...state, capabilities: ["test"] };
    const held = gate();
    let runs = 0;
    server.use(
      http.post("/api/translation/connections/:id/test", async () => {
        runs += 1;
        if (runs === 2) {
          await held.opened;
        }
        return HttpResponse.json({ ok: true, message: "", latency_ms: 820 });
      }),
    );
    const said = "OpenAI compatible, model-2: It works. Answered in 820 ms.";
    const region = () =>
      screen
        .getAllByRole("status")
        .find((item) => item.getAttribute("aria-live") === "polite")!;
    const user = userEvent.setup();
    await showPage();
    const test = screen.getByRole("button", {
      name: "Test OpenAI compatible, model-2",
    });
    await user.click(test);
    await waitFor(() => expect(region()).toHaveTextContent(said));
    await user.click(test);
    // The second test is running: the region holds nothing, so its answer will be a change.
    await waitFor(() => expect(runs).toBe(2));
    expect(region()).toBeEmptyDOMElement();
    held.open();
    await waitFor(() => expect(region()).toHaveTextContent(said));
  });
});

describe("Translation page, installing an engine in place", () => {
  const offer = {
    plugin_id: "other_engine",
    name: "Other Engine",
    version: "2.0.0",
    source: "official",
  };
  const withLogin = () =>
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {},
          translator: { default_score: 50, translator_info: true },
          auth: { type: "form", username: "admin", password: "***" },
        }),
      ),
      http.post("/api/system/account/confirm", () => {
        record("post", "confirm", null);
        return new HttpResponse(null, { status: 204 });
      }),
    );
  const askToInstall = async (user: ReturnType<typeof userEvent.setup>) => {
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Install Other Engine" }),
    );
    return screen.findByRole("dialog", { name: "Install Other Engine?" });
  };

  beforeEach(() => {
    state = { ...state, available_engines: [offer] };
    server.use(consentHandler());
  });

  it("asks for the password before the install goes out, where a login is set up", async () => {
    withLogin();
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        record("post", "install", await request.json());
        return HttpResponse.json({ job_id: null });
      }),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    // The server would refuse an install without a confirmed password: the page asks first.
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();
    expect(calls).toEqual([]);
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual(["confirm", "install"]),
    );
    expect(calls[1].body).toEqual({
      source: "official",
      id: "other_engine",
      version: "2.0.0",
      consent: plainSummary.digest,
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Install Other Engine?" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("asks again and sends the install once more when the server answers that the password must be confirmed", async () => {
    withLogin();
    // The page believes a confirmation is still good. The server does not.
    markPasswordConfirmed();
    let attempt = 0;
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        record("post", "install", await request.json());
        attempt += 1;
        return attempt === 1
          ? HttpResponse.json(
              { code: "confirm_password", message: "Confirm your password" },
              { status: 403 },
            )
          : HttpResponse.json({ job_id: null });
      }),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    await waitFor(() => expect(calls).toHaveLength(1));
    // Refused for want of a confirmation: the prompt comes up, with no failure shown for it.
    expect(
      await screen.findByText(/Confirm your password to install Other Engine/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/The install did not finish/),
    ).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "install",
        "confirm",
        "install",
      ]),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Install Other Engine?" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("installs nothing when the password question is closed without an answer", async () => {
    withLogin();
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.click(within(prompt).getByRole("button", { name: /^cancel$/i }));
    await waitFor(() =>
      expect(
        screen.queryByText("Confirm your password"),
      ).not.toBeInTheDocument(),
    );
    expect(calls).toEqual([]);
    // The install question is still there, and can be answered again.
    expect(
      await within(ask).findByRole("button", { name: "Install" }),
    ).toBeEnabled();
  });

  it("stays busy until the install has finished, so a second install cannot start", async () => {
    const held = gate();
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        record("post", "install", await request.json());
        await held.opened;
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    const install = await agree(user, ask);
    await waitFor(() => expect(calls).toHaveLength(1));
    // The request is still open: nothing can be pressed.
    expect(install).toBeDisabled();
    expect(within(ask).getByRole("button", { name: "Cancel" })).toBeDisabled();
    await user.click(install);
    await user.keyboard("{Escape}");
    expect(calls).toHaveLength(1);
    expect(
      screen.getByRole("dialog", { name: "Install Other Engine?" }),
    ).toBeVisible();
    held.open();
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Install Other Engine?" }),
      ).not.toBeInTheDocument(),
    );
    expect(calls).toHaveLength(1);
  });

  it("shows the server's own sentence when the refusal is plain text", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.text("This plugin is blocked on this machine.", {
          status: 400,
        }),
      ),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    expect(
      await within(ask).findByText("This plugin is blocked on this machine."),
    ).toBeVisible();
    // What the dialog showed is read again after a refusal, and it can be agreed to once it is back.
    await waitFor(() =>
      expect(
        within(ask).getByRole("button", { name: "Install" }),
      ).toBeEnabled(),
    );
  });
});

describe("Translation page, an engine installed and waiting for a restart", () => {
  it("shows the restart prompt and a pending status, not a plugin that is broken", async () => {
    state = {
      ...state,
      instances: [
        second,
        engine({
          plugin: {
            name: "AI Subtitle Translator",
            version: "2.2.0",
            active: false,
          },
        }),
      ],
    };
    server.use(
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({
          data: [
            {
              provider_id: "ai_translator",
              kind: "translator",
              state: "staged",
              needs_restart: true,
            },
          ],
        }),
      ),
    );
    await showPage();
    expect(
      await screen.findByText("1 plugin change is waiting for a restart."),
    ).toBeVisible();
    expect(screen.getAllByText("Pending restart").length).toBeGreaterThan(0);
    expect(screen.queryByText("Plugin not running")).not.toBeInTheDocument();
  });

  it("keeps the repair hint for a plugin the server does not say is waiting", async () => {
    state = {
      ...state,
      instances: [
        second,
        engine({
          plugin: {
            name: "AI Subtitle Translator",
            version: "2.2.0",
            active: false,
          },
        }),
      ],
    };
    server.use(
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({ data: [] }),
      ),
    );
    await showPage();
    expect(
      (await screen.findAllByText("Plugin not running")).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText(/waiting for a restart/)).not.toBeInTheDocument();
  });
});

describe("Translation page, a row whose switch is being written", () => {
  it("cannot be opened for editing until the switch has been saved", async () => {
    const held = gate();
    server.use(
      http.patch("/api/translation/connections/:id", async ({ params }) => {
        record("patch", "connection " + String(params.id), null);
        await held.opened;
        return HttpResponse.json({
          connection: connection(Number(params.id), { enabled: false }),
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const edit = screen.getByRole("button", {
      name: "Edit OpenAI compatible, model-1",
    });
    await user.click(screen.getByLabelText("Use OpenAI compatible, model-1"));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(edit).toBeDisabled();
    // The other row is not held back.
    expect(
      screen.getByRole("button", { name: "Edit OpenAI compatible, model-2" }),
    ).toBeEnabled();
    await user.click(edit);
    expect(screen.queryByLabelText("Base URL")).not.toBeInTheDocument();
    held.open();
    await waitFor(() => expect(edit).toBeEnabled());
  });
});

describe("Translation page, defaults after a save", () => {
  // A server that keeps the score it is sent, and answers the load after the save when let through.
  function settingsServer(reload: "held" | "fails") {
    const held = gate();
    let score = 50;
    let saved = false;
    server.use(
      http.get("/api/system/settings", async () => {
        if (saved && reload === "fails") {
          return new HttpResponse(null, { status: 500 });
        }
        if (saved) {
          await held.opened;
        }
        return HttpResponse.json({
          general: {},
          translator: {
            default_score: score,
            min_source_score: 90,
            translator_info: true,
            openrouter_max_concurrent: 2,
          },
        });
      }),
      http.post("/api/system/settings", async ({ request }) => {
        const text = await request.text();
        record("post", "settings", text);
        score = 65;
        saved = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    return held;
  }

  async function changeScore(user: ReturnType<typeof userEvent.setup>) {
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const score = await screen.findByLabelText(
      "Score of a translated subtitle",
    );
    await user.clear(score);
    await user.type(score, "65");
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    return score;
  }

  it("keeps showing what was sent until the saved values have been loaded back", async () => {
    const held = settingsServer("held");
    const user = userEvent.setup();
    const score = await changeScore(user);
    // Saved, and the load of the saved values is still open: the field does not fall back to 50.
    expect(score).toHaveValue("65");
    expect(score).toBeDisabled();
    held.open();
    await waitFor(() => expect(score).toBeEnabled());
    expect(score).toHaveValue("65");
    // The draft is gone: there is nothing left to save.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Save defaults" }),
      ).toBeDisabled(),
    );
    expect(
      screen.queryByRole("button", { name: "Discard changes" }),
    ).not.toBeInTheDocument();
  });

  it("says so and keeps what was sent when the saved values cannot be loaded back", async () => {
    settingsServer("fails");
    const user = userEvent.setup();
    const score = await changeScore(user);
    expect(
      await screen.findByText(
        "Your changes were saved, but the saved values could not be loaded back. The fields show what you sent.",
      ),
    ).toBeVisible();
    expect(score).toHaveValue("65");
    expect(score).toBeEnabled();
  });
});

describe("Translation page, an install whose answer is not a sentence", () => {
  const offer = {
    plugin_id: "other_engine",
    name: "Other Engine",
    version: "2.0.0",
    source: "official",
  };
  const askToInstall = async (user: ReturnType<typeof userEvent.setup>) => {
    state = { ...state, available_engines: [offer] };
    server.use(consentHandler());
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Install Other Engine" }),
    );
    return screen.findByRole("dialog", { name: "Install Other Engine?" });
  };

  it("does not show an error page from a proxy as if the server had said it", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.text(
          "<html><head><title>502 Bad Gateway</title></head><body><h1>502 Bad Gateway</h1></body></html>",
          { status: 502 },
        ),
      ),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    expect(await within(ask).findByText(FAILED_INSTALL)).toBeVisible();
    expect(within(ask).queryByText(/Bad Gateway/)).not.toBeInTheDocument();
    expect(within(ask).queryByText(/</)).not.toBeInTheDocument();
  });

  it("does not show a text body too long to be a sentence", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.text("Traceback line. ".repeat(40), { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    expect(await within(ask).findByText(FAILED_INSTALL)).toBeVisible();
    expect(within(ask).queryByText(/Traceback/)).not.toBeInTheDocument();
  });

  it("lists the engine once the install has finished, after one install", async () => {
    let installedOnServer = false;
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        record("post", "install", await request.json());
        installedOnServer = true;
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
      http.get("/api/translation/state", () =>
        HttpResponse.json(
          installedOnServer
            ? {
                ...state,
                available_engines: [],
                instances: [
                  ...state.instances,
                  { ...second, id: 9, name: "Other Engine" },
                ],
              }
            : state,
        ),
      ),
    );
    const user = userEvent.setup();
    const ask = await askToInstall(user);
    await agree(user, ask);
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Install Other Engine?" }),
      ).not.toBeInTheDocument(),
    );
    // One install, and the page now lists the engine.
    expect(calls.filter((call) => call.path === "install")).toHaveLength(1);
    expect(
      await screen.findByRole("radio", { name: "Other Engine" }),
    ).toBeInTheDocument();
  });
});

describe("Translation page, a switch write that is refused", () => {
  it("holds the row until what is saved now has been loaded", async () => {
    const held = gate();
    let holding = false;
    server.use(
      http.get("/api/translation/state", async () => {
        if (holding) {
          await held.opened;
        }
        return HttpResponse.json(state);
      }),
      http.patch("/api/translation/connections/:id", () => {
        record("patch", "connection", null);
        holding = true;
        return HttpResponse.json(
          {
            error: "stale_revision",
            message: "The back end changed since it was loaded.",
            revision: 4,
          },
          { status: 409 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const toggle = screen.getByLabelText("Use OpenAI compatible, model-1");
    const edit = screen.getByRole("button", {
      name: "Edit OpenAI compatible, model-1",
    });
    await user.click(toggle);
    expect(await screen.findByText(/was changed somewhere else/)).toBeVisible();
    // Refused, and the reload is still open: the row is not released yet.
    expect(toggle).toHaveAttribute("aria-busy", "true");
    expect(edit).toBeDisabled();
    await user.click(toggle);
    expect(calls).toHaveLength(1);
    held.open();
    await waitFor(() => expect(toggle).toHaveAttribute("aria-busy", "false"));
    expect(edit).toBeEnabled();
    expect(toggle).toBeChecked();
  });
});

describe("Translation page, defaults saved in part", () => {
  it("says the cost caps were not saved when only that write fails, and clears it when a retry starts", async () => {
    state = { ...state, capabilities: ["summary", "limits"] };
    const held = gate();
    let capsFail = true;
    const summary = (cap: number | null) => ({
      month: { key: "2026-11", spent: 3.21, cap },
      job_cap: 1,
      queue: { running: 0, pending: 0, limit: 2 },
    });
    server.use(
      http.get("/api/translation/summary", () =>
        HttpResponse.json(summary(20)),
      ),
      http.post("/api/system/settings", () => {
        record("post", "settings", null);
        return new HttpResponse(null, { status: 204 });
      }),
      http.put("/api/translation/limits", async ({ request }) => {
        const body = (await request.json()) as { month_cap: number | null };
        record("put", "limits", body);
        if (capsFail) {
          return new HttpResponse(null, { status: 500 });
        }
        await held.opened;
        return HttpResponse.json(summary(body.month_cap));
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const score = await screen.findByLabelText(
      "Score of a translated subtitle",
    );
    await user.clear(score);
    await user.type(score, "65");
    const month = await screen.findByLabelText("Cost cap per month");
    await user.clear(month);
    await user.type(month, "35");
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    const said =
      "Your other changes were saved. The cost caps were not saved. Try again.";
    expect(await screen.findByText(said)).toBeVisible();
    expect(calls.map((call) => call.path)).toEqual(["settings", "limits"]);
    // What was typed is still there to send again.
    expect(month).toHaveValue("$35");
    capsFail = false;
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    // The retry is on its way: the old message is gone before its answer arrives.
    await waitFor(() => expect(calls.at(-1)?.path).toBe("limits"));
    await waitFor(() => expect(calls).toHaveLength(4));
    expect(screen.queryByText(said)).not.toBeInTheDocument();
    held.open();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Save defaults" }),
      ).toBeDisabled(),
    );
    expect(screen.queryByText(said)).not.toBeInTheDocument();
  });
});

describe("Translation page, typing the defaults", () => {
  const summary = (job: number | null, month: number | null) => ({
    month: { key: "2026-11", spent: 3.21, cap: month },
    job_cap: job,
    queue: { running: 0, pending: 0, limit: 2 },
    limits_revision: 4,
  });

  it("keeps an amount with decimals as it is typed in both cap fields, and saves exactly that", async () => {
    state = { ...state, capabilities: ["summary", "limits"] };
    let saved = summary(1, 20);
    server.use(
      http.get("/api/translation/summary", () => HttpResponse.json(saved)),
      http.put("/api/translation/limits", async ({ request }) => {
        const body = (await request.json()) as {
          job_cap: number | null;
          month_cap: number | null;
        };
        record("put", "limits", body);
        saved = summary(body.job_cap, body.month_cap);
        return HttpResponse.json(saved);
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));

    // Typed key by key, as a person does. The field reports "0." before it reports 0.15, and that
    // half-typed amount has to stay in the field: dropped, the next keys would spell $15.
    const job = await screen.findByLabelText("Cost cap per job");
    await user.clear(job);
    await user.type(job, "0");
    expect(job).toHaveValue("$0");
    await user.type(job, ".");
    expect(job).toHaveValue("$0.");
    await user.type(job, "1");
    expect(job).toHaveValue("$0.1");
    await user.type(job, "5");
    expect(job).toHaveValue("$0.15");

    // An amount that passes through a zero after the point, and ends on one.
    const month = screen.getByLabelText("Cost cap per month");
    await user.clear(month);
    await user.type(month, "7.0");
    expect(month).toHaveValue("$7.0");
    await user.type(month, "5");
    expect(month).toHaveValue("$7.05");
    await user.type(month, "{Backspace}{Backspace}");
    expect(month).toHaveValue("$7.");
    await user.type(month, "50");
    expect(month).toHaveValue("$7.50");

    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ job_cap: 0.15, month_cap: 7.5 });
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Save defaults" }),
      ).toBeDisabled(),
    );
    expect(screen.getByLabelText("Cost cap per job")).toHaveValue("$0.15");
    expect(screen.getByLabelText("Cost cap per month")).toHaveValue("$7.5");
  });

  it("takes an emptied cap as no cap, and a cap that is not an amount is not saved", async () => {
    state = { ...state, capabilities: ["summary", "limits"] };
    server.use(
      http.get("/api/translation/summary", () =>
        HttpResponse.json(summary(1, 20)),
      ),
      http.put("/api/translation/limits", async ({ request }) => {
        const body = await request.json();
        record("put", "limits", body);
        return HttpResponse.json(summary(null, 20));
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const job = await screen.findByLabelText("Cost cap per job");
    await user.clear(job);
    // A point alone is not an amount yet.
    await user.type(job, ".");
    expect(
      screen.getByText("Enter an amount, or leave it empty for no cap."),
    ).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Save defaults" }),
    ).toBeDisabled();
    await user.type(job, "{Enter}");
    expect(calls).toHaveLength(0);
    await user.clear(job);
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ job_cap: null, month_cap: 20 });
  });

  it("does not save a score above 100 when Enter is pressed before the field is left", async () => {
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        record("post", "settings", await request.text());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const score = await screen.findByLabelText(
      "Score of a translated subtitle",
    );
    await user.clear(score);
    // The field brings 500 back to 100 when it is left. Enter submits the form without leaving it.
    await user.type(score, "500{Enter}");
    expect(screen.getByText("Enter a score from 0 to 100.")).toBeVisible();
    expect(
      screen.getByRole("button", { name: "Save defaults" }),
    ).toBeDisabled();
    expect(calls).toHaveLength(0);

    const atOnce = screen.getByLabelText("Translations at once");
    await user.clear(atOnce);
    await user.type(atOnce, "0{Enter}");
    expect(screen.getByText("Enter a number from 1 to 10.")).toBeVisible();
    expect(calls).toHaveLength(0);

    // In range again, Enter saves.
    await user.clear(atOnce);
    await user.type(atOnce, "4");
    await user.clear(score);
    await user.type(score, "100{Enter}");
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toContain("settings-translator-default_score");
    expect(calls[0].body).toContain("100");
    expect(calls[0].body).toContain(
      "settings-translator-openrouter_max_concurrent",
    );
  });
});

describe("Translation page, a quick start with the official engine switched off", () => {
  // The engine was turned off in the full page and has no back end yet, so a reload shows the first-run panel.
  const offEngine = () =>
    engine({ enabled: false, configured: false, connections: [] });

  const firstRun = (enable: (body: unknown) => Response | null) => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [offEngine()],
    };
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          const created = connection(3, { backend_type: "openrouter" });
          state = {
            ...state,
            instances: [{ ...state.instances[0], connections: [created] }],
          };
          return HttpResponse.json({ connection: created }, { status: 201 });
        },
      ),
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = await request.json();
        record("patch", "engine", body);
        const refused = enable(body);
        if (refused) {
          return refused;
        }
        const on = {
          ...state.instances[0],
          enabled: true,
          configured: true,
          revision: 4,
        };
        state = { ...state, instances: [on] };
        return HttpResponse.json({ engine: on, notices: [] });
      }),
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        state = { ...state, engine: 1 };
        return HttpResponse.json({
          engine: 1,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
  };

  it("turns the engine on before it selects it, so the start that is reported does translate", async () => {
    firstRun(() => null);
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    // The full page, with the engine picked, on, and ready.
    const group = await screen.findByRole("radiogroup", { name: "Engine" });
    expect(calls.map((call) => call.path)).toEqual([
      "connection",
      "engine",
      "pointers",
    ]);
    // It is turned on from the revision it has once the back end exists, before anything is selected.
    expect(calls[1].body).toEqual({ enabled: true, expected_revision: 3 });
    expect(calls[2].body).toEqual({ engine: 1 });
    expect(
      within(group).getByRole("radio", { name: /AI Subtitle Translator/ }),
    ).toBeChecked();
    expect(
      screen.getByRole("switch", {
        name: "Turn AI Subtitle Translator on or off",
      }),
    ).toBeChecked();
    expect(
      within(
        screen.getByRole("group", { name: "Translation status" }),
      ).getByText("Ready"),
    ).toBeVisible();
    expect(inputsHolding(SECRET)).toHaveLength(0);
  });

  it("shows the engine as on when it was turned on and could not be selected", async () => {
    firstRun(() => null);
    let pointerFails = true;
    server.use(
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
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    expect(
      await screen.findByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    expect(calls.map((call) => call.path)).toEqual([
      "connection",
      "engine",
      "pointers",
    ]);
    // The engine is on now, at a new revision, and nothing is selected. The full page has to show
    // that: an engine drawn as off, at the revision it had before, would refuse the next thing done to it.
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(screen.getByRole("button", { name: "Set it up by hand" }));
    await screen.findByRole("radiogroup", { name: "Engine" });
    const power = screen.getByRole("switch", {
      name: "Turn AI Subtitle Translator on or off",
    });
    expect(power).toBeChecked();
    // Its switch works from the revision the engine has now.
    pointerFails = false;
    await user.click(power);
    await waitFor(() => expect(calls).toHaveLength(4));
    expect(calls[3].body).toEqual({ enabled: false, expected_revision: 4 });
  });

  it("says the engine is still off when it cannot be turned on, selects nothing, and leads to its switch", async () => {
    // What the server answers while the engine's saved settings do not fit its plugin.
    firstRun(() =>
      HttpResponse.json(
        {
          error: "invalid",
          message: "The values are not valid.",
          field_errors: { credit_line: "is required" },
        },
        { status: 400 },
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    expect(
      await screen.findByText(
        /Your key is saved, but AI Subtitle Translator is turned off and cannot be turned on until its settings are checked\. Nothing translates yet\./,
      ),
    ).toBeVisible();
    // No success: the panel is still here, and the engine was not selected while it is off.
    expect(
      screen.getByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).toBeVisible();
    expect(calls.map((call) => call.path)).toEqual(["connection", "engine"]);
    expect(
      screen.queryByText(/the engine could not be switched on/),
    ).toBeNull();
    expect(inputsHolding(SECRET)).toHaveLength(0);

    // The way to the switch is in the message.
    await user.click(screen.getByRole("button", { name: "Open its set-up" }));
    await screen.findByRole("radiogroup", { name: "Engine" });
    const power = screen.getByRole("switch", {
      name: "Turn AI Subtitle Translator on or off",
    });
    expect(power).not.toBeChecked();
    // The back end that was saved is there, so nothing is typed twice.
    expect(
      screen.getByRole("button", { name: /^Edit OpenRouter/ }),
    ).toBeVisible();
  });

  it("says so too when the refusal names no setting, and a second press tries again without adding the back end twice", async () => {
    let refuse = true;
    firstRun(() =>
      refuse
        ? HttpResponse.json(
            {
              error: "plugin_unavailable",
              message: "The plugin could not check these settings.",
            },
            { status: 409 },
          )
        : null,
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    expect(
      await screen.findByText(
        /Your key is saved, but AI Subtitle Translator is turned off and could not be turned on\. Nothing translates yet\. Try again, or open its set-up and turn it on there\./,
      ),
    ).toBeVisible();
    refuse = false;
    await user.click(
      screen.getByRole("button", { name: "Switch the engine on" }),
    );
    await screen.findByRole("radiogroup", { name: "Engine" });
    expect(calls.map((call) => call.path)).toEqual([
      "connection",
      "engine",
      "engine",
      "pointers",
    ]);
  });
});
