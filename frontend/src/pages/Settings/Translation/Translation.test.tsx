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
import SettingsTranslationView from ".";

const SECRET = "sk-page-test-0123456789"; // pragma: allowlist secret

const connection = (
  id: number,
  patch: Partial<TranslationConnection> = {},
): TranslationConnection => ({
  id,
  instance_id: 1,
  position: id - 1,
  backend_type: "openai_compatible",
  plugin_id: "",
  model: `model-${id}`,
  enabled: true,
  revision: 1,
  awaiting_secrets: false,
  usable: true,
  settings_version: 1,
  values: { base_url: "http://lms:1234/v1", model: `model-${id}` },
  secrets: {},
  ...patch,
});

const engine = (patch: Partial<TranslationEngine> = {}): TranslationEngine => ({
  id: 1,
  plugin_id: "ai_translator",
  name: "AI Subtitle Translator",
  official: true,
  enabled: true,
  revision: 3,
  awaiting_secrets: false,
  configured: true,
  settings_version: 1,
  plugin_version: "2.2.0",
  plugin: { name: "AI Subtitle Translator", version: "2.2.0", active: true },
  schema: {
    settings_version: 1,
    endpoints: [],
    fields: [
      { key: "credit_line", type: "text", label: "Credit line" },
      { key: "service_token", type: "secret", label: "Service token" },
    ],
    backend_types: {
      openai_compatible: {
        label: "OpenAI compatible",
        description: "",
        endpoints: [],
        fields: [
          { key: "model", type: "options", label: "Model" },
          {
            key: "base_url",
            type: "text",
            label: "Base URL",
            destination: true,
          },
          { key: "api_key", type: "secret", label: "API key" },
        ],
      },
    },
  },
  values: { credit_line: "Translated by Bazarr+" },
  secrets: { service_token: { configured: true, last4: "WXYZ" } },
  connections: [connection(1), connection(2)],
  ...patch,
});

const second: TranslationEngine = engine({
  id: 2,
  plugin_id: "google_translate",
  name: "Google Translate",
  official: false,
  schema: null,
  connections: [],
  plugin: { name: "Google Translate", version: "1.0.0", active: true },
});

let state: TranslationState;
let calls: Array<{ method: string; path: string; body: unknown }>;

function record(method: string, path: string, body: unknown) {
  calls.push({ method, path, body });
}

beforeEach(() => {
  calls = [];
  state = {
    enabled: true,
    engine: 1,
    fallback_engine: 2,
    instances: [second, engine()],
  };
  server.use(
    http.get("/api/translation/state", () => HttpResponse.json(state)),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/settings", () => HttpResponse.json({})),
    http.post("/api/translation/engines/:id/options", () =>
      HttpResponse.json({ available: false, items: [], reason: "none" }),
    ),
  );
});

async function showPage() {
  customRender(<SettingsTranslationView />);
  return screen.findByText("Translation engine");
}

describe("Settings > Translation", () => {
  it("lists the official engine first, with its connections in order", async () => {
    await showPage();
    const headings = screen.getAllByRole("heading", { level: 3 });
    expect(headings.map((item) => item.textContent)).toEqual([
      "AI Subtitle Translator (Official)",
      "Google Translate",
    ]);
    expect(screen.getByText("OpenAI compatible, model-1")).toBeVisible();
    expect(screen.getByText("OpenAI compatible, model-2")).toBeVisible();
  });

  it("changes the engine through the pointers route and shows the notice", async () => {
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
    await showPage();
    const input = screen.getByRole("combobox", { name: "Translation engine" });
    await user.click(input);
    const listbox = (
      await screen.findAllByRole("listbox", { hidden: true })
    ).find(
      (box) => box.getAttribute("id") === input.getAttribute("aria-controls"),
    )!;
    await user.click(
      within(listbox).getByRole("option", {
        name: "Google Translate",
        hidden: true,
      }),
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
    expect(document.body.textContent).not.toContain(SECRET);
  });

  it("turns an engine that an update switched off back on, and the form saves on top of that", async () => {
    state = {
      ...state,
      instances: [
        second,
        engine({ enabled: false, awaiting_secrets: true, configured: false }),
      ],
    };
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
    const toggle = screen.getByRole("switch", { name: "Use this engine" });
    expect(toggle).not.toBeChecked();
    await user.click(toggle);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ enabled: true, expected_revision: 3 });
    await waitFor(() =>
      expect(
        screen.getByRole("switch", { name: "Use this engine" }),
      ).toBeChecked(),
    );
    // The switch moved the revision on; the form it left alone is still current for the next save.
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toMatchObject({ expected_revision: 4 });
  });

  it("says why an engine cannot be turned on yet", async () => {
    state = {
      ...state,
      instances: [second, engine({ enabled: false, awaiting_secrets: true })],
    };
    server.use(
      http.patch("/api/translation/engines/:id", () =>
        HttpResponse.json(
          { error: "invalid", message: "Region is required." },
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getByRole("switch", { name: "Use this engine" }));
    expect(await screen.findByText("Region is required.")).toBeVisible();
  });

  it("shows a field error beside its field and the conflict message", async () => {
    let answer: "invalid" | "stale" = "invalid";
    server.use(
      http.patch("/api/translation/engines/:id", () =>
        answer === "invalid"
          ? HttpResponse.json(
              {
                error: "invalid",
                message: "The values are not valid.",
                field_errors: { credit_line: "must be at most 5 characters" },
              },
              { status: 400 },
            )
          : HttpResponse.json(
              {
                error: "stale_revision",
                message: "The engine settings changed since they were loaded.",
              },
              { status: 409 },
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
    answer = "stale";
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    expect(
      await screen.findByText(
        /The engine settings changed since they were loaded\./,
      ),
    ).toBeVisible();
  });

  it("reorders back ends and sends every id in the new order", async () => {
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
  });

  it("switches a back end off and removes one after a second click", async () => {
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
    const row = screen
      .getAllByRole("listitem")
      .find((item) => within(item).queryByText("OpenAI compatible, model-2"))!;
    await user.click(within(row).getByRole("button", { name: "Remove" }));
    expect(calls).toHaveLength(1); // one click removes nothing
    await user.click(
      within(row).getByRole("button", { name: "Confirm remove" }),
    );
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
    const row = screen
      .getAllByRole("listitem")
      .find((item) => within(item).queryByText("OpenAI compatible, model-2"))!;
    await user.click(within(row).getByRole("button", { name: "Remove" }));
    await user.click(
      within(row).getByRole("button", { name: "Confirm remove" }),
    );
    expect(
      await screen.findByText("The back end changed since it was loaded."),
    ).toBeVisible();
    await waitFor(() => expect(loads).toBeGreaterThan(before));
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
                  destinations: ["http://192.168.1.5:1234/v1"],
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
    const dialog = await screen.findByRole("dialog", {
      name: "Add a back end",
    });
    await user.type(
      within(dialog).getByLabelText("Base URL"),
      "http://192.168.1.5:1234/v1",
    );
    await user.type(within(dialog).getByLabelText("API key"), SECRET);
    await user.click(
      within(dialog).getByRole("button", { name: "Add back end" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Send data to a new address?",
    });
    expect(within(ask).getByText("http://192.168.1.5:1234/v1")).toBeVisible();
    expect(calls).toHaveLength(1);
    expect(calls[0].body).toMatchObject({ accept_destinations: [] });
    await user.click(
      within(ask).getByRole("button", { name: "Send data there" }),
    );
    await waitFor(() => expect(calls).toHaveLength(2));
    expect(calls[1].body).toEqual({
      backend_type: "openai_compatible",
      values: { base_url: "http://192.168.1.5:1234/v1", api_key: SECRET },
      accept_destinations: ["http://192.168.1.5:1234/v1"],
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Add a back end" }),
      ).not.toBeInTheDocument(),
    );
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
    const dialog = await screen.findByRole("dialog", {
      name: "Add a back end",
    });
    await user.type(within(dialog).getByLabelText("Base URL"), "http://x:1/v1");
    await user.click(
      within(dialog).getByRole("button", { name: "Add back end" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Send data to a new address?",
    });
    await user.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", { name: "Send data to a new address?" }),
      ).not.toBeInTheDocument(),
    );
    expect(calls).toHaveLength(1);
  });

  it("falls back to a free text model field when the list cannot be fetched", async () => {
    const user = userEvent.setup();
    await showPage();
    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Edit back end" });
    const model = within(dialog).getByLabelText("Model");
    expect(model).toHaveValue("model-1");
    await user.clear(model);
    await user.type(model, "my/other-model");
    expect(model).toHaveValue("my/other-model");
  });

  it("says so when the page cannot load", async () => {
    server.use(
      http.get(
        "/api/translation/state",
        () => new HttpResponse(null, { status: 404 }),
      ),
    );
    customRender(<SettingsTranslationView />);
    expect(
      await screen.findByText("The translation settings could not be loaded."),
    ).toBeVisible();
  });

  it("keeps what was typed after a conflict, loads the new revision, and the next save goes through", async () => {
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
      await screen.findByText(/changed since they were loaded/),
    ).toBeVisible();
    expect(screen.getByText(/Your changes are kept/)).toBeVisible();
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
    await waitFor(() =>
      expect(screen.queryByText(/Your changes are kept/)).toBeVisible(),
    );
    // The state was loaded again, so the form now holds revision 7.
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(7));
    await waitFor(() =>
      expect(
        screen.queryByText(/Your changes are kept/),
      ).not.toBeInTheDocument(),
    );
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
    expect(
      await screen.findByText(/changed since they were loaded/),
    ).toBeVisible();
    expect(sent.map((body) => body.expected_revision)).toEqual([3]);
    expect(screen.getByLabelText("Credit line")).toHaveValue("Mine");
    // Saving again after seeing the conflict is the choice to overwrite.
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
    );
    await waitFor(() => expect(sent.at(-1)?.expected_revision).toBe(4));
    await waitFor(() =>
      expect(
        screen.queryByText(/Your changes are kept/),
      ).not.toBeInTheDocument(),
    );
    // A saved draft is at the saved revision, so the next edit saves without a conflict.
    await user.type(screen.getByLabelText("Credit line"), "!");
    await user.click(
      screen.getByRole("button", { name: "Save engine settings" }),
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
    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Edit back end" });
    const url = within(dialog).getByLabelText("Base URL");
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
      within(dialog).getByRole("button", { name: "Save back end" }),
    );
    expect(
      await within(dialog).findByText(/changed since it was loaded/),
    ).toBeVisible();
    await user.click(
      within(dialog).getByRole("button", { name: "Save back end" }),
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
      screen.queryByText("The translation settings could not be loaded."),
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

  it("asks for the model list again when the address changes", async () => {
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
    await user.click(screen.getAllByRole("button", { name: "Edit" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Edit back end" });
    await waitFor(() => expect(asked).toHaveLength(1));
    // A key is typed first, so there is something that could leak into a request.
    const typedKey = "sk-typed-0123456789-abcdef"; // pragma: allowlist secret
    await user.type(within(dialog).getByLabelText("API key"), typedKey);
    const url = within(dialog).getByLabelText("Base URL");
    await user.clear(url);
    await user.type(url, "http://other:9/v1");
    await waitFor(() => expect(asked.length).toBeGreaterThan(1));
    expect(asked.at(-1)?.values).toMatchObject({
      base_url: "http://other:9/v1",
    });
    // The field is still holding the key, and no request named the field or carried the key.
    expect(within(dialog).getByLabelText("API key")).toHaveValue(typedKey);
    const everything = JSON.stringify(asked);
    expect(everything).not.toContain("api_key");
    expect(everything).not.toContain(typedKey);
    expect(everything).not.toContain("sk-typed");
  });
});
