/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { act, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type {
  TranslationConnection,
  TranslationEngine,
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

let state: TranslationState;
let calls: Array<{ method: string; path: string; body: unknown }>;

function record(method: string, path: string, body: unknown) {
  calls.push({ method, path, body });
}

beforeEach(() => {
  calls = [];
  window.history.replaceState({}, "", "/");
  state = {
    enabled: true,
    engine: 1,
    fallback_engine: 2,
    instances: [second, engine()],
  };
  server.use(
    http.get("/api/translation/state", () => HttpResponse.json(state)),
    // The state lists no capabilities, so the page must not ask for any of these.
    http.get("/api/translation/summary", () => {
      record("get", "summary", null);
      return new HttpResponse(null, { status: 404 });
    }),
    http.get("/api/translation/queue", () => {
      record("get", "queue", null);
      return new HttpResponse(null, { status: 404 });
    }),
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

// The add menu's items are asked for with `hidden`: the menu hides itself when its button has no
// place on screen, and in these tests nothing has a size.
async function showPage() {
  customRender(<TranslationView />);
  return screen.findByRole("radiogroup", { name: "Engine" });
}

const row = (name: string) =>
  screen
    .getAllByRole("listitem")
    .find((item) =>
      within(item).queryByRole("button", { name: `Edit ${name}` }),
    )!;

async function openEditor(name: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: `Edit ${name}` }));
  return row(name);
}

describe("Translation page", () => {
  it("lists the official engine first, then installed engines, then None, with back ends in order", async () => {
    const group = await showPage();
    const radios = within(group).getAllByRole("radio");
    expect(
      radios.map((item) => item.getAttribute("aria-describedby")),
    ).toHaveLength(3);
    expect(radios[0]).toHaveAccessibleName(/AI Subtitle Translator/);
    expect(radios[0]).toHaveAccessibleName(/Official/);
    expect(radios[0]).toBeChecked();
    expect(radios[1]).toHaveAccessibleName("Google Translate");
    expect(radios[2]).toHaveAccessibleName("None");
    const names = screen
      .getAllByRole("button", { name: /^Edit / })
      .map((item) => item.getAttribute("aria-label"));
    expect(names).toEqual([
      "Edit OpenAI compatible, model-1",
      "Edit OpenAI compatible, model-2",
    ]);
  });

  it("picks an engine through the pointers route and shows the notice", async () => {
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: 2,
          fallback_engine: null,
          notices: [
            {
              code: "fallback_reset",
              message: "The fallback engine was reset.",
              instance_id: 2,
            },
          ],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    await user.click(
      within(group).getByRole("radio", { name: "Google Translate" }),
    );
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "pointers", body: { engine: 2 } },
      ]),
    );
    expect(
      await screen.findByText("The fallback engine was reset."),
    ).toBeVisible();
  });

  it("turns translation off with None", async () => {
    server.use(
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: null,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    await user.click(within(group).getByRole("radio", { name: "None" }));
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "pointers", body: { engine: null } },
      ]),
    );
  });

  it("shows only an update chip for a plugin update, linked to the hub", async () => {
    state = {
      ...state,
      instances: [
        second,
        engine({
          plugin: {
            name: "AI Subtitle Translator",
            version: "2.2.0",
            active: true,
            update_version: "2.3.0",
          },
        }),
      ],
    };
    await showPage();
    const chip = screen.getByRole("link", { name: "Update available" });
    expect(chip).toHaveAttribute("href", "/subtitle-hub?tab=updates");
    expect(
      screen.queryByRole("button", { name: /^Update/ }),
    ).not.toBeInTheDocument();
  });

  it("saves engine settings by typing, keeps the secret out of the screen, and sends the revision", async () => {
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        return HttpResponse.json({
          engine: engine({ revision: 4 }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Made with care");
    await user.type(
      screen.getByPlaceholderText(
        "Saved, ending WXYZ. Leave empty to keep it.",
      ),
      SECRET,
    );
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({
      expected_revision: 3,
      values: { credit_line: "Made with care", service_token: SECRET },
      clear_secrets: [],
      accept_destinations: [],
    });
    // Once saved, the secret is nowhere on the page: not in the text, and not left in its input.
    await waitFor(() => expect(inputsHolding(SECRET)).toHaveLength(0));
    expect(document.body.textContent).not.toContain(SECRET);
    expect(
      screen.getByPlaceholderText(
        "Saved, ending WXYZ. Leave empty to keep it.",
      ),
    ).toHaveValue("");
  });

  // An engine that a plugin update turned off: off, and flagged for a look.
  const heldBack = () =>
    engine({ enabled: false, awaiting_secrets: true, configured: false });
  // An engine whose plugin declares nothing to set.
  const bare = (patch: Partial<TranslationEngine> = {}) =>
    engine({
      values: {},
      secrets: {},
      connections: [],
      schema: {
        settings_version: 1,
        endpoints: [],
        fields: [],
        backend_types: {},
      },
      ...patch,
    });
  const powerSwitch = (name = "AI Subtitle Translator") =>
    screen.getByRole("switch", { name: `Turn ${name} on or off` });

  it("says under its name why an engine an update turned off is off, and what to do", async () => {
    state = { ...state, engine: null, instances: [second, heldBack()] };
    const group = await showPage();
    expect(powerSwitch()).not.toBeChecked();
    expect(
      screen.getByText("Turned off until its settings are checked"),
    ).toBeVisible();
    expect(
      screen.getByText(/usually because a plugin update changed/),
    ).toHaveTextContent(
      "Check the engine settings below and save them, then turn it on with the switch beside its name.",
    );
    // The flag it carries is the one a changed address sets, and nothing here blames an address.
    expect(screen.queryByText(/address changed/)).toBeNull();
    expect(screen.queryByText("Enter the keys again")).toBeNull();
    // Its card in the list says the same, in short.
    expect(within(group).getByText("Needs a review")).toBeVisible();
    // No engine is picked any more, and the line at the top of the page names the one to look at.
    expect(
      within(
        screen.getByRole("group", { name: "Translation status" }),
      ).getByText(
        "AI Subtitle Translator was turned off and needs a look. Open its set-up to turn it on again.",
      ),
    ).toBeVisible();
  });

  it("turns an engine that an update switched off back on, and the form saves on top of that", async () => {
    state = { ...state, instances: [second, heldBack()] };
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { enabled?: boolean };
        record("patch", "engine", body);
        if (body.enabled) {
          state = { ...state, instances: [second, engine({ revision: 4 })] };
        }
        return HttpResponse.json({
          engine: engine({ revision: 4 }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    expect(powerSwitch()).not.toBeChecked();
    await user.click(powerSwitch());
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ enabled: true, expected_revision: 3 });
    await waitFor(() => expect(powerSwitch()).toBeChecked());
    await waitFor(() =>
      expect(
        screen.queryByText("Turned off until its settings are checked"),
      ).toBeNull(),
    );
    // The switch moved the revision on; the form it left alone is still current for the next save.
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toMatchObject({ expected_revision: 4 });
  });

  it("keeps the switch beside the name of an engine with nothing to set up, on or off", async () => {
    // The only engine on the page, so every line found is its own.
    state = { ...state, fallback_engine: null, instances: [bare()] };
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { enabled: boolean };
        record("patch", "engine", body);
        const saved = bare({ enabled: body.enabled, revision: 4 });
        state = { ...state, engine: null, instances: [saved] };
        return HttpResponse.json({ engine: saved, notices: [] });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    // On: the switch is there, with nothing else to do.
    expect(powerSwitch()).toBeChecked();
    expect(screen.getByText("Nothing to set up")).toBeVisible();
    expect(screen.getByText("This engine works as it is.")).toBeVisible();
    expect(
      screen.queryByRole("heading", { name: "Engine settings" }),
    ).toBeNull();
    await user.click(powerSwitch());
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ enabled: false, expected_revision: 3 });
    // Off: the same switch, and the line under the name says how to bring it back.
    await waitFor(() => expect(powerSwitch()).not.toBeChecked());
    expect(
      await screen.findAllByText(
        "This engine is off. Turn it on to translate with it.",
      ),
    ).not.toHaveLength(0);
    expect(
      screen.getByText(
        "This engine has no settings. Once it is on, it works as it is.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Save engine settings" }),
    ).toBeNull();
  });

  it("can turn on an engine with no settings that an update turned off", async () => {
    const held = bare({ enabled: false, awaiting_secrets: true });
    state = { ...state, instances: [second, held] };
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        return HttpResponse.json({
          engine: { ...held, enabled: true, revision: 4 },
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    expect(screen.getByText("This engine was turned off")).toBeVisible();
    expect(
      screen.getByText(
        "A plugin update usually does this. Turn it on with the switch beside its name.",
      ),
    ).toBeVisible();
    await user.click(powerSwitch());
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ enabled: true, expected_revision: 3 });
  });

  it("marks the settings that keep an engine from being turned on, and puts the switch back", async () => {
    state = { ...state, instances: [second, heldBack()] };
    server.use(
      // What the server answers while the saved settings do not fit the plugin: the fields, and a
      // sentence that names none of them.
      http.patch("/api/translation/engines/:id", () =>
        HttpResponse.json(
          {
            error: "invalid",
            message: "The values are not valid.",
            field_errors: { credit_line: "is required" },
          },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(powerSwitch());
    expect(
      await screen.findByText(
        "It cannot be turned on yet: a setting below needs fixing. Save the settings, then turn it on.",
      ),
    ).toBeVisible();
    expect(screen.getByText("is required")).toBeVisible();
    expect(screen.queryByText("The values are not valid.")).toBeNull();
    await waitFor(() => expect(powerSwitch()).not.toBeChecked());
  });

  it("says in the server's words why a switch was refused, or in its own when there are none", async () => {
    state = { ...state, instances: [second, heldBack()] };
    let answer: () => Response = () =>
      HttpResponse.json(
        {
          error: "plugin_unavailable",
          message: "The plugin could not check these settings.",
        },
        { status: 409 },
      );
    server.use(http.patch("/api/translation/engines/:id", () => answer()));
    const user = userEvent.setup();
    await showPage();
    await user.click(powerSwitch());
    expect(
      await screen.findByText("The plugin could not check these settings."),
    ).toBeVisible();
    await waitFor(() => expect(powerSwitch()).not.toBeChecked());
    answer = () => new HttpResponse(null, { status: 500 });
    await user.click(powerSwitch());
    expect(
      await screen.findByText("The engine could not be turned on. Try again."),
    ).toBeVisible();
    expect(
      screen.queryByText("The plugin could not check these settings."),
    ).toBeNull();
  });

  it("says that no engine is picked after the one that translates is turned off", async () => {
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        const saved = engine({
          enabled: false,
          configured: false,
          revision: 4,
        });
        state = { ...state, engine: null, instances: [second, saved] };
        return HttpResponse.json({
          engine: saved,
          notices: [
            {
              code: "engine_reset",
              message:
                "The selected translation engine was turned off, so no engine is selected.",
              instance_id: 1,
            },
          ],
        });
      }),
    );
    const user = userEvent.setup();
    const group = await showPage();
    await user.click(powerSwitch());
    expect(
      await screen.findByText(
        "The selected translation engine was turned off, so no engine is selected.",
      ),
    ).toBeVisible();
    expect(calls[0].body).toEqual({ enabled: false, expected_revision: 3 });
    // Turned off by hand: it is plainly off, with nothing to review.
    await waitFor(() => expect(within(group).getByText("Off")).toBeVisible());
    expect(
      screen.queryByText("Turned off until its settings are checked"),
    ).toBeNull();
  });

  it("holds the settings form while the switch is written, and the switch while the form saves", async () => {
    let release: () => void = () => undefined;
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json({
          engine: engine({ revision: 4 }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const save = () =>
      screen.getByRole("button", { name: "Save engine settings" });
    await user.click(powerSwitch());
    await waitFor(() => expect(calls).toHaveLength(1));
    // The switch shows where it was put while the write is on its way, and a second press adds no request.
    expect(powerSwitch()).not.toBeChecked();
    expect(powerSwitch()).toHaveAttribute("aria-busy", "true");
    expect(save()).toBeDisabled();
    await user.click(powerSwitch());
    expect(calls).toHaveLength(1);
    release();
    await waitFor(() => expect(save()).toBeEnabled());
    await user.click(save());
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(powerSwitch()).toBeDisabled();
    release();
    await waitFor(() => expect(powerSwitch()).toBeEnabled());
  });

  it("shows a field error beside its field", async () => {
    server.use(
      http.patch("/api/translation/engines/:id", () =>
        HttpResponse.json(
          {
            error: "invalid",
            message: "The values are not valid.",
            field_errors: { credit_line: "must be at most 5 characters" },
          },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    expect(
      await screen.findByText("must be at most 5 characters"),
    ).toBeVisible();
  });

  it("moves a back end with its button, sends every id in the new order and says where it went", async () => {
    server.use(
      http.put(
        "/api/translation/engines/:id/connections/order",
        async ({ request }) => {
          record("put", "order", await request.json());
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
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "put", path: "order", body: { order: [2, 1] } },
      ]),
    );
    expect(
      await screen.findByText(
        "OpenAI compatible, model-1 moved to position 2 of 2.",
      ),
    ).toBeInTheDocument();
  });

  it("puts the list back and says why when the new order is refused", async () => {
    server.use(
      http.put("/api/translation/engines/:id/connections/order", () =>
        HttpResponse.json(
          {
            error: "bad_request",
            message: "The order must list every back end once.",
          },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", {
        name: "Move OpenAI compatible, model-2 up",
      }),
    );
    expect(
      await screen.findByText("The order must list every back end once."),
    ).toBeVisible();
    await waitFor(() =>
      expect(
        screen
          .getAllByRole("button", { name: /^Edit / })
          .map((item) => item.getAttribute("aria-label")),
      ).toEqual([
        "Edit OpenAI compatible, model-1",
        "Edit OpenAI compatible, model-2",
      ]),
    );
  });

  it("switches a back end off, and removes one only after the question is answered", async () => {
    server.use(
      http.patch(
        "/api/translation/connections/:id",
        async ({ request, params }) => {
          record(
            "patch",
            `connection ${String(params.id)}`,
            await request.json(),
          );
          return HttpResponse.json({
            connection: connection(Number(params.id)),
          });
        },
      ),
      http.delete("/api/translation/connections/:id", ({ params, request }) => {
        // The removal carries the revision the list was loaded with.
        record(
          "delete",
          `connection ${String(params.id)}`,
          new URL(request.url).searchParams.get("expected_revision"),
        );
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByLabelText("Use OpenAI compatible, model-1"));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0]).toEqual({
      method: "patch",
      path: "connection 1",
      body: { enabled: false, expected_revision: 1 },
    });
    const editor = await openEditor("OpenAI compatible, model-2");
    await user.click(
      within(editor).getByRole("button", { name: "Remove back end" }),
    );
    expect(calls).toHaveLength(1); // asking removes nothing
    expect(
      within(editor).getByText("Remove OpenAI compatible and delete its key?"),
    ).toBeVisible();
    await user.click(within(editor).getByRole("button", { name: "Remove" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].method).toBe("delete");
    expect(calls[1].body).toBe("1");
  });

  it("reloads the state when a removal is refused because the back end changed", async () => {
    let loads = 0;
    server.use(
      http.get("/api/translation/state", () => {
        loads += 1;
        return HttpResponse.json(state);
      }),
      http.delete("/api/translation/connections/:id", () =>
        HttpResponse.json(
          {
            error: "stale_revision",
            message: "The back end changed since it was loaded.",
            revision: 4,
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    const before = loads;
    const editor = await openEditor("OpenAI compatible, model-2");
    await user.click(
      within(editor).getByRole("button", { name: "Remove back end" }),
    );
    await user.click(within(editor).getByRole("button", { name: "Remove" }));
    expect(
      await screen.findByText("The back end changed since it was loaded."),
    ).toBeVisible();
    await waitFor(() => expect(loads).toBeGreaterThan(before));
  });

  it("offers every back-end type and every preset, and a preset fills the address", async () => {
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Add a back end" }));
    const items = await screen.findAllByRole("menuitem", { hidden: true });
    expect(items.map((item) => item.textContent)).toEqual([
      "OpenRouter",
      "LM Studio",
      "Custom",
    ]);
    await user.click(items[1]);
    expect(
      await screen.findByRole("heading", { name: "New back end: LM Studio" }),
    ).toBeVisible();
    expect(screen.getByLabelText("Base URL")).toHaveValue(
      "http://host.docker.internal:1234/v1",
    );
  });

  it("asks before sending data to a new address, and sends the answer with the retry", async () => {
    let attempt = 0;
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          const body = await request.json();
          record("post", "connection", body);
          attempt += 1;
          return attempt === 1
            ? HttpResponse.json(
                {
                  error: "consent_required",
                  message: "These addresses are new.",
                  destinations: ["http://192.0.2.10:1234/v1"],
                },
                { status: 409 },
              )
            : HttpResponse.json({ connection: connection(3) }, { status: 201 });
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Add a back end" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Custom", hidden: true }),
    );
    await user.type(
      await screen.findByLabelText("Base URL"),
      "http://192.0.2.10:1234/v1",
    );
    await user.type(screen.getByLabelText("API key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Add back end" }));
    const ask = await screen.findByRole("dialog", {
      name: "Send subtitles to a new address?",
    });
    // The question fades in: visible once it has.
    await waitFor(() =>
      expect(within(ask).getByText("http://192.0.2.10:1234/v1")).toBeVisible(),
    );
    expect(
      within(ask).getByText(/never sent to a changed address/),
    ).toBeVisible();
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toMatchObject({ accept_destinations: [] });
    await user.click(
      within(ask).getByRole("button", { name: "Send to this address" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toEqual({
      backend_type: "openai_compatible",
      values: {
        preset: "custom",
        base_url: "http://192.0.2.10:1234/v1",
        api_key: SECRET,
      },
      accept_destinations: ["http://192.0.2.10:1234/v1"],
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("heading", { name: /^New back end/ }),
      ).not.toBeInTheDocument(),
    );
    expect(inputsHolding(SECRET)).toHaveLength(0);
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("declining the question sends nothing more", async () => {
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          return HttpResponse.json(
            {
              error: "consent_required",
              message: "new",
              destinations: ["http://x:1/v1"],
            },
            { status: 409 },
          );
        },
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("button", { name: "Add a back end" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Custom", hidden: true }),
    );
    await user.type(await screen.findByLabelText("Base URL"), "http://x:1/v1");
    await user.click(screen.getByRole("button", { name: "Add back end" }));
    const ask = await screen.findByRole("dialog", {
      name: "Send subtitles to a new address?",
    });
    await user.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", {
          name: "Send subtitles to a new address?",
        }),
      ).not.toBeInTheDocument(),
    );
    expect(calls).toHaveLength(1);
    // The form is still open with what was typed.
    expect(screen.getByLabelText("Base URL")).toHaveValue("http://x:1/v1");
  });

  it("says a back end needs its key again after its address changed", async () => {
    state = {
      ...state,
      instances: [
        second,
        engine({
          connections: [
            connection(1, { awaiting_secrets: true, usable: false }),
            connection(2),
          ],
        }),
      ],
    };
    await showPage();
    const item = row("OpenAI compatible, model-1");
    expect(within(item).getByText("Needs its key again")).toBeVisible();
    const editor = await openEditor("OpenAI compatible, model-1");
    expect(
      within(editor).getByText(/was not sent to the new address/),
    ).toBeVisible();
  });

  // A back end that a plugin update turned off: off and flagged, with its key as it was.
  const heldBackEnd = (
    id: number,
    patch: Partial<TranslationConnection> = {},
  ) =>
    connection(id, {
      enabled: false,
      awaiting_secrets: true,
      usable: false,
      secrets: { api_key: { configured: true, last4: "WXYZ" } },
      ...patch,
    });
  const withBackEnds = (...connections: TranslationConnection[]) => {
    state = { ...state, instances: [second, engine({ connections })] };
  };

  it("marks a back end an update turned off, and says above the list what to do", async () => {
    withBackEnds(connection(1), heldBackEnd(2));
    await showPage();
    const item = row("OpenAI compatible, model-2");
    expect(within(item).getByText("Needs a review")).toBeVisible();
    expect(within(item).getByText("Off")).toBeVisible();
    // The flag is the one a changed address sets, and nothing here blames an address or a key.
    expect(within(item).queryByText("Needs its key again")).toBeNull();
    expect(within(item).getByText("Key ending WXYZ")).toBeVisible();
    expect(
      screen.getByText(
        "A back end was turned off until its settings are checked",
      ),
    ).toBeVisible();
    expect(
      screen.getByText(/usually because a plugin update changed/),
    ).toHaveTextContent(
      "Open OpenAI compatible, model-2 with Edit, check its settings and save them, then turn it on with its switch.",
    );
    // Its form says the same at its top, so the line above the list steps aside while it is open.
    const editor = await openEditor("OpenAI compatible, model-2");
    expect(
      within(editor).getByText("Turned off until its settings are checked"),
    ).toBeVisible();
    expect(
      within(editor).getByText(/usually because a plugin update changed/),
    ).toHaveTextContent(
      "Check the settings below and save them, then turn it on with the switch on its row.",
    );
    expect(within(editor).queryByText("Enter the key again")).toBeNull();
    expect(
      screen.queryByText(
        "A back end was turned off until its settings are checked",
      ),
    ).toBeNull();
  });

  it("counts the back ends an update turned off, and says so for the engine when none is left on", async () => {
    withBackEnds(heldBackEnd(1), heldBackEnd(2));
    const group = await showPage();
    expect(
      screen.getByText(
        "2 back ends were turned off until their settings are checked",
      ),
    ).toBeVisible();
    expect(
      screen.getByText(/Open each one marked Needs a review with Edit/),
    ).toBeVisible();
    // The engine cannot take a job, and its card and the line at the top say why, not "turn one on".
    const why =
      "No back end is on. 2 were turned off until their settings are checked, usually after a plugin update. Open them under Back ends.";
    expect(within(group).getByText(why)).toBeVisible();
    expect(
      within(
        screen.getByRole("group", { name: "Translation status" }),
      ).getByText(why),
    ).toBeVisible();
    expect(screen.queryByText("No back end is on")).toBeNull();
  });

  it("keeps the key hint for a back end that is off and whose address changed", async () => {
    withBackEnds(
      connection(1),
      // The server marks the saved key as stale: the reason is the key, whatever the switch says.
      heldBackEnd(2, {
        secrets: { api_key: { configured: true, last4: "WXYZ", stale: true } },
      }),
    );
    await showPage();
    const item = row("OpenAI compatible, model-2");
    expect(within(item).getByText("Needs its key again")).toBeVisible();
    expect(within(item).getByText("Off")).toBeVisible();
    expect(within(item).getByText("Key needed again")).toBeVisible();
    expect(within(item).queryByText("Needs a review")).toBeNull();
    expect(screen.queryByText(/turned off until/)).toBeNull();
    const editor = await openEditor("OpenAI compatible, model-2");
    expect(within(editor).getByText("Enter the key again")).toBeVisible();
    expect(within(editor).queryByText(/Turned off until/)).toBeNull();
  });

  it("says where to go when a back end cannot be turned on yet, and marks the setting in its form", async () => {
    withBackEnds(connection(1), heldBackEnd(2));
    server.use(
      // What the server answers while the saved settings do not fit the back end's type.
      http.patch("/api/translation/connections/:id", async ({ request }) => {
        record("patch", "connection", await request.json());
        return HttpResponse.json(
          {
            error: "invalid",
            message: "The values are not valid.",
            field_errors: { base_url: "is required" },
          },
          { status: 400 },
        );
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("switch", { name: "Use OpenAI compatible, model-2" }),
    );
    expect(
      await screen.findByText(
        "OpenAI compatible, model-2 cannot be turned on yet: a setting needs fixing. Open it with Edit and save its settings, then turn it on.",
      ),
    ).toBeVisible();
    expect(calls[0].body).toEqual({ enabled: true, expected_revision: 1 });
    expect(screen.queryByText("The values are not valid.")).toBeNull();
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Use OpenAI compatible, model-2" }),
      ).not.toBeChecked(),
    );
    const editor = await openEditor("OpenAI compatible, model-2");
    expect(within(editor).getByText("is required")).toBeVisible();
  });

  it("offers no test of a back end an update turned off, and says why", async () => {
    state = { ...state, capabilities: ["test"] };
    withBackEnds(connection(1), heldBackEnd(2));
    server.use(
      http.post("/api/translation/connections/:id/test", () => {
        record("post", "test", null);
        return HttpResponse.json({ ok: true, message: "", latency_ms: 1 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    // A test uses what is saved, and the server would answer it with a changed address.
    const held = screen.getByRole("button", {
      name: "Test OpenAI compatible, model-2",
    });
    expect(held).toBeDisabled();
    expect(held).toHaveAccessibleDescription(
      "A test uses what is saved. Check and save this back end's settings first.",
    );
    const other = screen.getByRole("button", {
      name: "Test OpenAI compatible, model-1",
    });
    expect(other).toBeEnabled();
    await user.click(other);
    expect(
      await screen.findByText("It works. Answered in 1 ms."),
    ).toBeVisible();
    expect(calls).toHaveLength(1);
  });

  it("says to check and save an engine that is not set up while its address already shows a value", async () => {
    state = {
      ...state,
      engine: 5,
      fallback_engine: null,
      instances: [lingarr({ configured: false })],
    };
    const group = await showPage();
    expect(within(group).getByText("Not set up")).toBeVisible();
    expect(screen.getByLabelText(/Lingarr URL/)).toHaveValue(
      "http://lingarr:9876",
    );
    expect(
      screen.getAllByText("Check its settings and save them.", {
        exact: false,
      }).length,
    ).toBeGreaterThan(0);
  });

  it("says a held-back back end of a type the plugin dropped can only be removed", async () => {
    withBackEnds(connection(1), heldBackEnd(2, { backend_type: "retired" }));
    await showPage();
    const item = row("retired, model-2");
    expect(within(item).getByText("No longer offered")).toBeVisible();
    // Nothing to check, so it is not counted among the ones to review.
    expect(screen.queryByText(/turned off until/)).toBeNull();
    const editor = await openEditor("retired, model-2");
    expect(
      within(editor).getByText("This kind of back end is no longer offered"),
    ).toBeVisible();
    expect(
      within(editor).getByText(
        /Remove this back end and add another in its place/,
      ),
    ).toBeVisible();
    expect(
      within(editor).queryByRole("button", { name: "Save back end" }),
    ).toBeNull();
    expect(
      within(editor).getByRole("button", { name: "Remove back end" }),
    ).toBeVisible();
  });

  it("falls back to a free text model field when the list cannot be fetched", async () => {
    const user = userEvent.setup();
    await showPage();
    const editor = await openEditor("OpenAI compatible, model-1");
    const model = within(editor).getByLabelText("Model");
    expect(model).toHaveValue("model-1");
    await user.clear(model);
    await user.type(model, "my/other-model");
    expect(model).toHaveValue("my/other-model");
  });

  it("says so when the page cannot load, and loads on Try again", async () => {
    let fail = true;
    server.use(
      http.get("/api/translation/state", () =>
        fail
          ? new HttpResponse(null, { status: 500 })
          : HttpResponse.json(state),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    expect(
      await screen.findByText("Translation could not be loaded"),
    ).toBeVisible();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(
      await screen.findByRole("radiogroup", { name: "Engine" }),
    ).toBeVisible();
  });

  it("keeps what was typed after a conflict, and Save my version sends the new revision", async () => {
    let revision = 3;
    const sent: Array<{ expected_revision?: number }> = [];
    server.use(
      http.get("/api/translation/state", () =>
        HttpResponse.json({
          ...state,
          instances: [second, engine({ revision })],
        }),
      ),
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { expected_revision?: number };
        sent.push(body);
        if (body.expected_revision !== revision) {
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
          engine: engine({ revision }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    revision = 7; // someone else saved
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Mine");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    expect(
      await screen.findByText(
        "These settings changed somewhere else since you opened this form",
      ),
    ).toBeVisible();
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
    // Overwriting is never a second click on Save: that button is gone until the choice is made.
    expect(
      screen.queryByRole("button", { name: "Save engine settings" }),
    ).not.toBeInTheDocument();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    // The state was loaded again, so the form now knows revision 7.
    await user.click(screen.getByRole("button", { name: "Save my version" }));
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(7));
    await waitFor(() =>
      expect(
        screen.queryByText(/changed somewhere else/),
      ).not.toBeInTheDocument(),
    );
  });

  it("Discard and reload replaces the draft with what is saved now", async () => {
    let saved = "Translated by Bazarr+";
    server.use(
      http.get("/api/translation/state", () =>
        HttpResponse.json({
          ...state,
          instances: [
            second,
            engine({ revision: 9, values: { credit_line: saved } }),
          ],
        }),
      ),
      http.patch("/api/translation/engines/:id", () =>
        HttpResponse.json(
          {
            error: "stale_revision",
            message: "The engine settings changed since they were loaded.",
            revision: 9,
          },
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    saved = "Theirs";
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Mine");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await screen.findByText(/changed somewhere else/);
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(
      screen.getByRole("button", { name: "Discard and reload" }),
    );
    await waitFor(() =>
      expect(screen.getByLabelText("Credit line")).toHaveValue("Theirs"),
    );
    expect(
      screen.getByRole("button", { name: "Save engine settings" }),
    ).toBeVisible();
  });

  it("sends the revision the draft was loaded from, even after the list refreshed", async () => {
    let revision = 3;
    let loads = 0;
    const sent: Array<{ expected_revision?: number }> = [];
    server.use(
      http.get("/api/translation/state", () => {
        loads += 1;
        return HttpResponse.json({
          ...state,
          instances: [second, engine({ revision })],
        });
      }),
      http.patch("/api/translation/connections/:id", ({ params }) =>
        HttpResponse.json({ connection: connection(Number(params.id)) }),
      ),
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        const body = (await request.json()) as { expected_revision?: number };
        sent.push(body);
        if (body.expected_revision !== revision) {
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
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Mine");
    // Another tab saves, then something on this page refreshes the list: the engine prop now says 4.
    revision = 4;
    await user.click(screen.getByLabelText("Use OpenAI compatible, model-1"));
    await waitFor(() => expect(loads).toBe(2));
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    // The draft still belongs to revision 3, so the save is refused instead of overwriting revision 4.
    expect(await screen.findByText(/changed somewhere else/)).toBeVisible();
    expect(sent.map((body) => body.expected_revision)).toEqual([3]);
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(screen.getByRole("button", { name: "Save my version" }));
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(4));
    // A saved draft is at the saved revision, so the next edit saves without a conflict.
    await user.type(await screen.findByLabelText("Credit line"), "!");
    await user.click(
      await screen.findByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(5));
  });

  it("keeps a back end draft at the revision it opened with when the list refreshes", async () => {
    const sent: Array<{ expected_revision?: number }> = [];
    server.use(
      http.patch("/api/translation/connections/:id", async ({ request }) => {
        const body = (await request.json()) as { expected_revision?: number };
        sent.push(body);
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
    const editor = await openEditor("OpenAI compatible, model-1");
    const url = within(editor).getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "http://other:9/v1");
    // Another tab saved this back end and the list refreshed under the open editor.
    state = {
      ...state,
      instances: [
        second,
        engine({
          connections: [connection(1, { revision: 2 }), connection(2)],
        }),
      ],
    };
    await act(async () => {
      await queryClient.invalidateQueries();
    });
    await user.click(
      within(editor).getByRole("button", { name: "Save back end" }),
    );
    expect(
      await within(editor).findByText(
        "This back end changed somewhere else since you opened this form",
      ),
    ).toBeVisible();
    expect(within(editor).getByLabelText("Base URL")).toHaveValue(
      "http://other:9/v1",
    );
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    await user.click(
      within(editor).getByRole("button", { name: "Save my version" }),
    );
    await waitFor(() => expect(sent).toHaveLength(2));
    expect(sent.map((body) => body.expected_revision)).toEqual([1, 2]);
  });

  it("saves the order a reorderable setting shows even when it was never touched", async () => {
    const withOrder = engine();
    withOrder.schema!.fields.push({
      key: "order",
      type: "orderable_list",
      label: "Order",
      items: [
        { value: "a", label: "Alpha" },
        { value: "b", label: "Beta" },
      ],
    });
    state = { ...state, instances: [second, withOrder] };
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        return HttpResponse.json({ engine: withOrder, notices: [] });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    expect(screen.getByText("Alpha")).toBeVisible();
    await user.type(screen.getByLabelText("Credit line"), "!");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ values: { order: ["a", "b"] } });
  });

  it("keeps the forms and what was typed when a refresh fails, and says so", async () => {
    const user = userEvent.setup();
    await showPage();
    const credit = screen.getByLabelText("Credit line");
    await user.clear(credit);
    await user.type(credit, "Half written");
    server.use(
      http.get(
        "/api/translation/state",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    await act(async () => {
      await queryClient.invalidateQueries();
    });
    expect(await screen.findByText(/could not be refreshed/)).toBeVisible();
    // The page was not replaced: the same form is still there with the same text in it.
    expect(screen.getByLabelText("Credit line")).toHaveValue("Half written");
    expect(
      screen.queryByText("Translation could not be loaded"),
    ).not.toBeInTheDocument();
  });

  it("clears a single stored secret on request and says it is pending", async () => {
    server.use(
      http.patch("/api/translation/engines/:id", async ({ request }) => {
        record("patch", "engine", await request.json());
        return HttpResponse.json({
          engine: engine({ revision: 4 }),
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Clear saved Service token" }),
    );
    expect(
      screen.getByPlaceholderText(
        "The saved value will be removed when you save",
      ),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ clear_secrets: ["service_token"] });
  });

  it("asks for the model list again when the address changes, and never with the key", async () => {
    const asked: Array<{ values: Record<string, unknown> }> = [];
    server.use(
      http.post("/api/translation/engines/:id/options", async ({ request }) => {
        asked.push(
          (await request.json()) as { values: Record<string, unknown> },
        );
        return HttpResponse.json({
          available: false,
          items: [],
          reason: "none",
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    const editor = await openEditor("OpenAI compatible, model-1");
    await waitFor(() => expect(asked).toHaveLength(1));
    // A key is typed first, so there is something that could leak into a request.
    const typedKey = "sk-typed-0123456789-abcdef"; // pragma: allowlist secret
    await user.type(within(editor).getByLabelText("API key"), typedKey);
    const url = within(editor).getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "http://other:9/v1");
    await waitFor(() => expect(asked.length).toBeGreaterThan(1));
    expect(asked.at(-1)?.values).toMatchObject({
      base_url: "http://other:9/v1",
    });
    // The field is still holding the key, and no request named the field or carried the key.
    expect(within(editor).getByLabelText("API key")).toHaveValue(typedKey);
    const everything = JSON.stringify(asked);
    expect(everything).not.toContain("api_key");
    expect(everything).not.toContain(typedKey);
    expect(everything).not.toContain("sk-typed");
  });

  it("tests a back end and shows the answer under its row", async () => {
    state = { ...state, capabilities: ["test"] };
    server.use(
      http.post("/api/translation/connections/:id/test", ({ params }) => {
        record("post", `test ${String(params.id)}`, null);
        return HttpResponse.json({ ok: true, message: "ok", latency_ms: 820 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Test OpenAI compatible, model-2" }),
    );
    expect(
      await within(row("OpenAI compatible, model-2")).findByText(
        "It works. Answered in 820 ms.",
      ),
    ).toBeVisible();
    expect(calls).toEqual([{ method: "post", path: "test 2", body: null }]);
  });

  it("shows the reason when a test fails", async () => {
    state = { ...state, capabilities: ["test"] };
    server.use(
      http.post("/api/translation/connections/:id/test", () =>
        HttpResponse.json({
          ok: false,
          message: "No answer from http://lms:1234. Check that it is running.",
        }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Test OpenAI compatible, model-1" }),
    );
    expect(
      await screen.findByText(
        "No answer from http://lms:1234. Check that it is running.",
      ),
    ).toBeVisible();
  });

  it("shows the sentence for a test the server refused before sending anything", async () => {
    state = { ...state, capabilities: ["test"] };
    server.use(
      http.post("/api/translation/connections/:id/test", () =>
        HttpResponse.json({
          ok: false,
          message:
            "A test of this back end is already running. Wait for it to finish.",
          latency_ms: null,
        }),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(
      screen.getByRole("button", { name: "Test OpenAI compatible, model-1" }),
    );
    const tested = row("OpenAI compatible, model-1");
    expect(
      await within(tested).findByText(
        "A test of this back end is already running. Wait for it to finish.",
      ),
    ).toBeVisible();
    // This answer names no outcome, as a server that does not send one: it reads as a failed test.
    expect(within(tested).getByTestId("test-result")).toHaveAttribute(
      "data-tone",
      "danger",
    );
    expect(screen.queryByText(/It works/)).not.toBeInTheDocument();
  });

  it("shows a test the engine could not take as not tested yet, and keeps the failure style for a back end that failed", async () => {
    state = { ...state, capabilities: ["test"] };
    // What the server answers, in turn: the engine is busy, the engine has not started, an answer with
    // no sentence, the back end did not answer, and a saved key that may not be sent.
    const answers = [
      {
        ok: false,
        message:
          "The translation engine is busy translating. Try the test again in a moment.",
        latency_ms: null,
        outcome: "not_ready",
      },
      {
        ok: false,
        message:
          "The translation engine could not take the test. Try again in a moment.",
        latency_ms: null,
        outcome: "not_ready",
      },
      { ok: false, message: "", latency_ms: null, outcome: "not_ready" },
      {
        ok: false,
        message: "No answer from http://lms:1234. Check that it is running.",
        latency_ms: null,
        outcome: "failed",
      },
      {
        ok: false,
        message:
          "The address changed after the key was saved, so the saved key is not sent to the new one. Enter the key again and save.",
        latency_ms: null,
        outcome: "refused",
      },
    ];
    let asked = 0;
    server.use(
      http.post("/api/translation/connections/:id/test", () =>
        HttpResponse.json(answers[asked++]),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    const name = "OpenAI compatible, model-1";
    const test = () =>
      user.click(screen.getByRole("button", { name: `Test ${name}` }));
    // The answer under the row once it reads `text`: the element that carries its tone.
    const shown = async (text: string) => {
      await within(row(name)).findByText(text);
      return within(row(name)).getByTestId("test-result");
    };

    await test();
    const busy =
      "Not tested yet. The translation engine is busy translating. Try the test again in a moment.";
    expect(await shown(busy)).toHaveAttribute("data-tone", "waiting");
    // It is said aloud the same way, with the back end's name.
    expect(screen.getByText(`${name}: ${busy}`)).toBeInTheDocument();

    await test();
    expect(
      await shown(
        "Not tested yet. The translation engine could not take the test. Try again in a moment.",
      ),
    ).toHaveAttribute("data-tone", "waiting");

    await test();
    expect(
      await shown(
        "Not tested yet. The engine could not take the test just now. Try again in a moment.",
      ),
    ).toHaveAttribute("data-tone", "waiting");

    await test();
    expect(
      await shown("No answer from http://lms:1234. Check that it is running."),
    ).toHaveAttribute("data-tone", "danger");
    expect(screen.queryByText(/Not tested yet/)).toBeNull();

    // Nothing was sent here either, but something has to be changed first: that is not "try again".
    await test();
    expect(
      await shown(
        "The address changed after the key was saved, so the saved key is not sent to the new one. Enter the key again and save.",
      ),
    ).toHaveAttribute("data-tone", "danger");
    expect(asked).toBe(5);
  });

  it("shows no Test, spend, queue or caps, and asks for none of them, on a server that lists no capabilities", async () => {
    const user = userEvent.setup();
    await showPage();
    const band = screen.getByRole("group", { name: "Translation status" });
    expect(within(band).getByText("AI Subtitle Translator")).toBeVisible();
    expect(within(band).queryByText("This month")).not.toBeInTheDocument();
    expect(within(band).queryByText("Queue")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^Test / }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    await screen.findByLabelText("Score of a translated subtitle");
    expect(
      screen.queryByLabelText("Cost cap per month"),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    await screen.findByText("Nothing has been translated yet");
    expect(screen.queryByText("Waiting")).not.toBeInTheDocument();
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    // Not one request went to a route the server did not list.
    expect(calls).toEqual([]);
  });

  it("shows the queue and the month's spend against its cap", async () => {
    state = { ...state, capabilities: ["summary"] };
    server.use(
      http.get("/api/translation/summary", () =>
        HttpResponse.json({
          month: { key: "2026-11", spent: 3.21, cap: 20 },
          job_cap: 1,
          queue: { running: 1, pending: 2, limit: 2 },
        }),
      ),
    );
    await showPage();
    const band = screen.getByRole("group", { name: "Translation status" });
    expect(await within(band).findByText("$3.21")).toBeVisible();
    expect(within(band).getByText("of $20.00")).toBeVisible();
    expect(within(band).getByText("1 running")).toBeVisible();
    expect(within(band).getByText("2 waiting")).toBeVisible();
    expect(
      within(band).getByRole("meter", {
        name: "Spend this month against the cap",
      }),
    ).toHaveAttribute("aria-valuetext", "$3.21 of $20.00");
  });

  it("says what the selected engine still needs", async () => {
    state = {
      ...state,
      instances: [second, engine({ connections: [] })],
    };
    await showPage();
    const band = screen.getByRole("group", { name: "Translation status" });
    expect(within(band).getByText("Needs a back end")).toBeVisible();
    expect(screen.getByText("No back ends yet")).toBeVisible();
  });
});

describe("Translation page, first run", () => {
  beforeEach(() => {
    state = {
      enabled: true,
      engine: null,
      fallback_engine: null,
      instances: [engine({ configured: false, connections: [] })],
      available_engines: [
        {
          plugin_id: "google_translate",
          name: "Google Translate",
          version: "1.0.0",
          source: "official",
          free: true,
          destinations: ["https://translate.googleapis.com"],
        },
      ],
    };
  });

  it("starts translating from one typed key: the back end first, then the engine", async () => {
    server.use(
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          return HttpResponse.json(
            { connection: connection(3) },
            { status: 201 },
          );
        },
      ),
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({
          engine: 1,
          fallback_engine: null,
          notices: [],
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    expect(
      await screen.findByRole("heading", {
        name: "Translate the subtitles you cannot find",
      }),
    ).toBeVisible();
    // Only the type a key alone sets up is offered; the one with an address is not.
    expect(screen.queryByText("OpenAI compatible")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Get a key" })).toHaveAttribute(
      "href",
      "https://example.test/keys",
    );
    await user.type(screen.getByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls).toEqual([
      {
        method: "post",
        path: "connection",
        body: {
          backend_type: "openrouter",
          values: { api_key: SECRET },
          accept_destinations: [],
        },
      },
      { method: "put", path: "pointers", body: { engine: 1 } },
    ]);
    // The key was stored with the first request: it is not on the page any more, in text or in an input.
    expect(document.body.textContent).not.toContain(SECRET);
    expect(inputsHolding(SECRET)).toHaveLength(0);
    expect(screen.getByLabelText("OpenRouter key")).toHaveValue("");
  });

  it("asks for the key before sending anything", async () => {
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.click(
      await screen.findByRole("button", { name: "Start translating" }),
    );
    expect(screen.getByText("Type the key first.")).toBeVisible();
    expect(calls).toHaveLength(0);
  });

  it("shows a refused key beside the field and does not select the engine", async () => {
    server.use(
      http.post("/api/translation/engines/:id/connections", () =>
        HttpResponse.json(
          {
            error: "invalid",
            message: "The values are not valid.",
            field_errors: { api_key: "is not a key this service accepts" },
          },
          { status: 400 },
        ),
      ),
      http.put("/api/translation/pointers", async ({ request }) => {
        record("put", "pointers", await request.json());
        return HttpResponse.json({ engine: 1, fallback_engine: null });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.type(await screen.findByLabelText("OpenRouter key"), "nope");
    await user.click(screen.getByRole("button", { name: "Start translating" }));
    expect(
      await screen.findByText("is not a key this service accepts"),
    ).toBeVisible();
    expect(calls).toHaveLength(0);
  });

  it("installs a free engine in place, after saying what leaves the machine", async () => {
    server.use(
      http.post("/api/provider-hub/installations", async ({ request }) => {
        record("post", "install", await request.json());
        return HttpResponse.json({ job_id: null });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.click(
      await screen.findByRole("button", { name: "Install Google Translate" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Install Google Translate?",
    });
    expect(
      within(ask).getByText("https://translate.googleapis.com"),
    ).toBeVisible();
    expect(calls).toHaveLength(0);
    await user.click(within(ask).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(calls).toEqual([
        {
          method: "post",
          path: "install",
          body: {
            source: "official",
            provider_id: "google_translate",
            version: "1.0.0",
          },
        },
      ]),
    );
  });

  it("opens the full page with nothing selected on Set it up by hand", async () => {
    const user = userEvent.setup();
    customRender(<TranslationView />);
    await user.click(
      await screen.findByRole("button", { name: "Set it up by hand" }),
    );
    const group = await screen.findByRole("radiogroup", { name: "Engine" });
    expect(within(group).getByRole("radio", { name: "None" })).toBeChecked();
    // The engine that is not installed is offered in the list, with Install.
    expect(
      within(group).getByRole("button", { name: "Install Google Translate" }),
    ).toBeVisible();
  });
});

describe("Translation page, defaults and activity", () => {
  it("saves only the defaults that were changed, typed", async () => {
    const posted: string[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        posted.push(await request.text());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const score = await screen.findByLabelText(
      "Score of a translated subtitle",
    );
    expect(score).toHaveValue("50");
    expect(
      screen.getByRole("button", { name: "Save defaults" }),
    ).toBeDisabled();
    // No caps without a server that reports them.
    expect(
      screen.queryByLabelText("Cost cap per month"),
    ).not.toBeInTheDocument();
    await user.clear(score);
    await user.type(score, "65");
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    await waitFor(() => expect(posted).toHaveLength(1));
    expect(posted[0]).toContain("settings-translator-default_score");
    expect(posted[0]).toContain("65");
    expect(posted[0]).not.toContain("min_source_score");
    expect(posted[0]).not.toContain("translator_info");
  });

  it("saves the caps through the limits route, an emptied cap as none", async () => {
    state = { ...state, capabilities: ["summary", "limits"] };
    server.use(
      http.get("/api/translation/summary", () =>
        HttpResponse.json({
          month: { key: "2026-11", spent: 3.21, cap: 20 },
          job_cap: 1,
          queue: { running: 0, pending: 0, limit: 2 },
        }),
      ),
      http.put("/api/translation/limits", async ({ request }) => {
        const body = (await request.json()) as {
          month_cap: number | null;
          job_cap: number | null;
        };
        record("put", "limits", body);
        return HttpResponse.json({
          month: { key: "2026-11", spent: 3.21, cap: body.month_cap },
          job_cap: body.job_cap,
          queue: { running: 0, pending: 0, limit: 2 },
        });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Defaults" }));
    const month = await screen.findByLabelText("Cost cap per month");
    await user.clear(month);
    await user.type(month, "35");
    await user.clear(screen.getByLabelText("Cost cap per job"));
    await user.click(screen.getByRole("button", { name: "Save defaults" }));
    await waitFor(() =>
      expect(calls).toEqual([
        {
          method: "put",
          path: "limits",
          body: { month_cap: 35, job_cap: null },
        },
      ]),
    );
  });

  it("lists what was translated and opens a job's detail", async () => {
    server.use(
      http.get("/api/translation/history", ({ request }) => {
        const status = new URL(request.url).searchParams.get("status");
        const rows = [
          job(1, { status: "running", finished_at: null }),
          job(2, { media_title: "Dune: Part Two" }),
          job(3, {
            media_title: "Flow",
            status: "failed",
            error_class: "content_error",
            error_detail: "The model returned 211 lines for 214.",
          }),
        ].filter((item) => !status || item.status === status);
        return HttpResponse.json({ data: rows, total: rows.length });
      }),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    const now = await screen.findByRole("list", { name: "Translating now" });
    expect(within(now).getByText("Film 1")).toBeVisible();
    const history = screen.getByRole("list", { name: "History" });
    // A running job is listed once, above.
    expect(within(history).queryByText("Film 1")).not.toBeInTheDocument();
    expect(within(history).getByText("Dune: Part Two")).toBeVisible();
    expect(
      within(history).getAllByText(/English to Hungarian/)[0],
    ).toBeVisible();
    await user.click(within(history).getByRole("button", { name: /Flow/ }));
    expect(
      await screen.findByText(
        "The model returned 211 lines for 214. (content_error)",
      ),
    ).toBeVisible();
  });

  it("says so when nothing has been translated yet", async () => {
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("tab", { name: "Activity" }));
    expect(
      await screen.findByText("Nothing has been translated yet"),
    ).toBeVisible();
  });
});
