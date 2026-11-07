/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
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
        "The engine settings changed since they were loaded.",
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
      http.delete("/api/translation/connections/:id", ({ params }) => {
        record("delete", `connection ${String(params.id)}`, null);
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
      body: { enabled: false },
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
});
