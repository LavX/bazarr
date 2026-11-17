/* eslint-disable camelcase -- API fixtures use the server's field names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  connection,
  engine,
  inputsHolding,
  lingarr,
  second,
  SECRET,
} from "@/pages/Translation/fixtures";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import type {
  TranslationAvailableEngine,
  TranslationEngine,
  TranslationState,
} from "@/types/translation";
import TranslationStep from "./TranslationStep";
import { NOTHING_WRITTEN } from "./types";

const onNext = vi.fn();
const onBack = vi.fn();

const calls: Array<{ method: string; path: string; body: unknown }> = [];
const record = (method: string, path: string, body: unknown) =>
  calls.push({ method, path, body });

const OFFICIAL_OFFER: TranslationAvailableEngine = {
  plugin_id: "ai_translator",
  name: "AI Subtitle Translator",
  version: "2.2.0",
  source: "official",
  official: true,
};
const GOOGLE_OFFER: TranslationAvailableEngine = {
  plugin_id: "google_translate",
  name: "Google Translate",
  version: "1.0.0",
  source: "official",
  free: true,
  destinations: ["https://translate.googleapis.com"],
};

/** The official engine with a Gemini type beside OpenRouter, both needing only a key. */
function aiEngine(patch: Partial<TranslationEngine> = {}) {
  const base = engine({ configured: false, connections: [] });
  return engine({
    configured: false,
    connections: [],
    schema: {
      ...base.schema!,
      backend_types: {
        ...base.schema!.backend_types,
        gemini: {
          label: "Gemini",
          description: "",
          endpoints: ["https://generativelanguage.googleapis.com"],
          fields: [
            {
              key: "model",
              type: "options",
              label: "Model",
              default: "gemini-2.5-flash",
            },
            {
              key: "api_keys",
              type: "key_list",
              label: "API keys",
              secret: true,
            },
          ],
        },
      },
    },
    ...patch,
  });
}

function serve(state: TranslationState) {
  server.use(
    http.get("/api/translation/state", () => HttpResponse.json(state)),
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
}

const firstRun = (patch: Partial<TranslationState> = {}): TranslationState => ({
  enabled: true,
  engine: null,
  fallback_engine: null,
  instances: [aiEngine()],
  available_engines: [GOOGLE_OFFER],
  ...patch,
});

beforeEach(() => {
  vi.clearAllMocks();
  calls.length = 0;
});

describe("TranslationStep", () => {
  it("starts translating from one typed OpenRouter key, then moves on", async () => {
    serve(firstRun());
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} onBack={onBack} />);

    // Two services, both needing a key alone: the reader picks one.
    const service = await screen.findByRole("radiogroup", { name: "Service" });
    expect(
      within(service).getByRole("radio", { name: "OpenRouter" }),
    ).toBeChecked();
    expect(
      within(service).getByRole("radio", { name: "Gemini" }),
    ).not.toBeChecked();
    // The type that needs an address is not a setup choice.
    expect(screen.queryByText("OpenAI compatible")).not.toBeInTheDocument();

    await user.type(screen.getByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    await waitFor(() => expect(onNext).toHaveBeenCalledTimes(1));
    // An answer: nothing is passed, so the rail draws the step done.
    expect(onNext).toHaveBeenCalledWith();
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
    // The key was stored with the first request and is not left on the page.
    expect(document.body.textContent).not.toContain(SECRET);
    expect(inputsHolding(SECRET)).toHaveLength(0);
  });

  it("starts with a Gemini key when Gemini is picked", async () => {
    serve(firstRun());
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    const service = await screen.findByRole("radiogroup", { name: "Service" });
    await user.click(within(service).getByRole("radio", { name: "Gemini" }));
    await user.type(screen.getByLabelText("Gemini key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    await waitFor(() => expect(onNext).toHaveBeenCalledTimes(1));
    expect(calls[0]).toEqual({
      method: "post",
      path: "connection",
      body: {
        backend_type: "gemini",
        // A field that holds several keys takes this one as its first.
        values: { api_keys: [SECRET] },
        accept_destinations: [],
      },
    });
  });

  it("asks for the key and moves nowhere when it is empty", async () => {
    serve(firstRun());
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: "Start translating" }),
    );

    expect(await screen.findByText("Type the key first.")).toBeVisible();
    expect(calls).toEqual([]);
    expect(onNext).not.toHaveBeenCalled();
  });

  it("continues with nothing written as a walk past, never as an answer", async () => {
    serve(firstRun());
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} onBack={onBack} />);

    await user.click(
      await screen.findByRole("button", {
        name: "Continue without translation",
      }),
    );

    expect(onNext).toHaveBeenCalledWith(NOTHING_WRITTEN);
    expect(calls).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("shows the install actions disabled, each with the reason, when nothing is installed", async () => {
    serve(
      firstRun({
        instances: [],
        available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
      }),
    );
    customRender(<TranslationStep onNext={onNext} />);

    const official = await screen.findByRole("button", {
      name: "Install AI Subtitle Translator",
    });
    const google = screen.getByRole("button", {
      name: "Install Google Translate",
    });
    // Shown and not pressable, never hidden.
    expect(official).toBeDisabled();
    expect(google).toBeDisabled();
    // The reason is on the page and tied to its button.
    for (const button of [official, google]) {
      expect(button).toHaveAccessibleDescription(
        /not available yet\. Install it in the Subtitle Hub/,
      );
    }
    // Nothing that sends or saves is offered while there is no engine.
    expect(
      screen.queryByRole("button", { name: "Start translating" }),
    ).not.toBeInTheDocument();
  });

  it("says Google Translate is not in the catalog while the catalog has no entry for it", async () => {
    serve(firstRun({ available_engines: [OFFICIAL_OFFER] }));
    customRender(<TranslationStep onNext={onNext} />);

    const google = await screen.findByRole("button", {
      name: "Install Google Translate",
    });
    expect(google).toBeDisabled();
    expect(google).toHaveAccessibleDescription(/not in the plugin catalog yet/);
  });

  it("offers an installed Google Translate as it is and selects it", async () => {
    serve(firstRun({ instances: [aiEngine(), second] }));
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: "Use Google Translate" }),
    );

    await waitFor(() => expect(onNext).toHaveBeenCalledWith());
    expect(calls).toEqual([
      { method: "put", path: "pointers", body: { engine: 2 } },
    ]);
    expect(
      screen.queryByRole("button", { name: "Install Google Translate" }),
    ).not.toBeInTheDocument();
  });

  it("does not offer Lingarr, which stays a legacy engine", async () => {
    serve(firstRun({ instances: [aiEngine(), second, lingarr()] }));
    customRender(<TranslationStep onNext={onNext} />);

    await screen.findByRole("button", { name: "Use Google Translate" });
    expect(screen.queryByText("Lingarr")).not.toBeInTheDocument();
  });

  it("explains an official engine that is installed and cannot run", async () => {
    serve(
      firstRun({
        instances: [
          aiEngine({
            plugin: {
              name: "AI Subtitle Translator",
              version: "2.2.0",
              active: false,
            },
          }),
        ],
      }),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(await screen.findByText("Plugin not running")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: "Start translating" }),
    ).not.toBeInTheDocument();
  });

  it("lets the reader continue when the page's state cannot be loaded", async () => {
    server.use(
      http.get(
        "/api/translation/state",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    expect(
      await screen.findByText("Translation could not be loaded"),
    ).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Continue without translation" }),
    );
    expect(onNext).toHaveBeenCalledWith(NOTHING_WRITTEN);
  });
});
