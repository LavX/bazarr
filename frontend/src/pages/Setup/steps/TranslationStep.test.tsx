/* eslint-disable camelcase -- API fixtures use the server's field names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { plainSummary } from "@/components/plugin-consent/fixtures";
import {
  connection,
  engine,
  engineInstallations,
  inputsHolding,
  lingarr,
  second,
  SECRET,
} from "@/pages/Translation/fixtures";
import { act, customRender, screen, waitFor, within } from "@/tests";
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
    http.get("/api/plugins/installations", () =>
      HttpResponse.json({ data: engineInstallations(state.instances) }),
    ),
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

  it("opens the consent dialog from the install actions, and sends nothing before it is agreed to", async () => {
    serve(
      firstRun({
        instances: [],
        available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
      }),
    );
    server.use(
      http.get("/api/plugins/catalog/consent", ({ request }) => {
        record(
          "get",
          "consent",
          Object.fromEntries(new URL(request.url).searchParams),
        );
        return HttpResponse.json(plainSummary);
      }),
      http.post("/api/plugins/installations", async ({ request }) => {
        record("post", "install", await request.json());
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    const official = await screen.findByRole("button", {
      name: "Install AI Subtitle Translator",
    });
    const google = screen.getByRole("button", {
      name: "Install Google Translate",
    });
    // Both can be pressed, and neither carries a reason to wait.
    expect(official).toBeEnabled();
    expect(google).toBeEnabled();
    expect(google).not.toHaveAccessibleDescription();
    // Nothing that sends or saves is offered while there is no engine.
    expect(
      screen.queryByRole("button", { name: "Start translating" }),
    ).not.toBeInTheDocument();

    await user.click(google);
    const ask = await screen.findByRole("dialog", {
      name: "Install Google Translate?",
    });
    await within(ask).findByText("Where it comes from");
    expect(calls).toEqual([
      {
        method: "get",
        path: "consent",
        body: { source: "official", id: "google_translate", version: "1.0.0" },
      },
    ]);
    await user.click(within(ask).getByRole("button", { name: "Install" }));
    await waitFor(() =>
      expect(calls.filter((call) => call.path === "install")).toEqual([
        {
          method: "post",
          path: "install",
          body: {
            source: "official",
            id: "google_translate",
            version: "1.0.0",
            consent: plainSummary.digest,
          },
        },
      ]),
    );
  });

  it("shows the server's refusal in the dialog and installs nothing when the set changed", async () => {
    serve(
      firstRun({
        instances: [],
        available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
      }),
    );
    server.use(
      http.get("/api/plugins/catalog/consent", () =>
        HttpResponse.json(plainSummary),
      ),
      http.post("/api/plugins/installations", () =>
        HttpResponse.text(
          "What this install would add has changed since it was shown to you. Review it again before you agree.",
          { status: 409 },
        ),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: "Install Google Translate" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Install Google Translate?",
    });
    const agree = await within(ask).findByRole("button", { name: "Install" });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);
    expect(
      await within(ask).findByText(/has changed since it was shown to you/),
    ).toBeVisible();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("holds the step, its Back included, while the consent dialog is open, and lets go once it is closed", async () => {
    serve(
      firstRun({
        instances: [],
        available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
      }),
    );
    server.use(
      http.get("/api/plugins/catalog/consent", () =>
        HttpResponse.json(plainSummary),
      ),
      http.post("/api/plugins/installations", () =>
        HttpResponse.text("What this install would add has changed.", {
          status: 409,
        }),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} onBack={onBack} />);

    await user.click(
      await screen.findByRole("button", { name: "Install Google Translate" }),
    );
    const ask = await screen.findByRole("dialog", {
      name: "Install Google Translate?",
    });
    const back = screen.getByRole("button", { name: "Back", hidden: true });
    expect(back).toBeDisabled();
    // A refusal is in the dialog, and the step stays held while it is there to be read.
    const agree = await within(ask).findByRole("button", { name: "Install" });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);
    expect(await within(ask).findByText(/has changed/)).toBeInTheDocument();
    expect(back).toBeDisabled();
    await user.click(within(ask).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(back).toBeEnabled());
  });

  it("says an engine that was installed is waiting for a restart, with the restart prompt, not that its plugin is broken", async () => {
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
        available_engines: [{ ...GOOGLE_OFFER }],
      }),
    );
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
            {
              provider_id: "google_translate",
              kind: "translator",
              state: "staged",
              needs_restart: true,
            },
          ],
        }),
      ),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(
      await screen.findByText("2 plugin changes are waiting for a restart."),
    ).toBeVisible();
    expect(screen.getAllByText("Pending restart").length).toBeGreaterThan(1);
    expect(screen.queryByText("Plugin not running")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Install Google Translate" }),
    ).not.toBeInTheDocument();
  });

  it("shows an engine removed while Bazarr+ runs as Removing, with no restart prompt and no repair hint", async () => {
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
        available_engines: [GOOGLE_OFFER],
      }),
    );
    server.use(
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({
          data: [
            {
              provider_id: "ai_translator",
              kind: "translator",
              state: "removed",
              active_version: "1.0.0",
              needs_restart: false,
            },
          ],
        }),
      ),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(await screen.findByText("Removing")).toBeVisible();
    expect(
      screen.getByText(
        /This plugin is being removed\. It goes when the calls it is running end/,
      ),
    ).toBeVisible();
    expect(screen.queryByText("Plugin not running")).not.toBeInTheDocument();
    expect(screen.queryByText(/waiting for a restart/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Install AI Subtitle Translator" }),
    ).not.toBeInTheDocument();

    let refreshed = 0;
    server.use(
      http.get("/api/translation/state", () => {
        refreshed++;
        return HttpResponse.json(
          firstRun({
            instances: [
              aiEngine({ plugin: null, schema: null, official: false }),
            ],
            available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
          }),
        );
      }),
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({ data: [] }),
      ),
    );
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.Plugins, "installations"],
    });

    expect(await screen.findByText("Plugin not running")).toBeVisible();
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Install AI Subtitle Translator" }),
      ).toBeEnabled(),
    );

    server.use(
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({
          data: [
            {
              provider_id: "subtitles",
              kind: "subtitle_provider",
              state: "active",
            },
          ],
        }),
      ),
    );
    await act(() =>
      queryClient.invalidateQueries({
        queryKey: [QueryKeys.Plugins, "installations"],
      }),
    );
    expect(refreshed).toBe(1);
  });

  it("refreshes an initially cached engine missing from installations", async () => {
    let reads = 0;
    const cached = firstRun();
    server.use(
      http.get("/api/translation/state", () => {
        reads++;
        return HttpResponse.json(
          reads === 1
            ? cached
            : firstRun({
                instances: [
                  aiEngine({ plugin: null, schema: null, official: false }),
                ],
                available_engines: [OFFICIAL_OFFER, GOOGLE_OFFER],
              }),
        );
      }),
      http.get("/api/plugins/installations", () =>
        HttpResponse.json({ data: [] }),
      ),
    );
    customRender(<TranslationStep onNext={onNext} />);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Install AI Subtitle Translator" }),
      ).toBeEnabled(),
    );
    expect(reads).toBe(2);
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
  });

  it.each([
    { when: "before reentry", finishesDuringEntry: false, expectedReads: 2 },
    {
      when: "between the entry replies",
      finishesDuringEntry: true,
      expectedReads: 3,
    },
  ])(
    "loads the reinstall offer when delete-data removal finishes $when",
    async ({ finishesDuringEntry, expectedReads }) => {
      let reads = 0;
      let finished = false;
      let returning = false;
      const installations = Promise.withResolvers<void>();
      const stateKey = [QueryKeys.Translation, "state"];
      const installationsKey = [
        QueryKeys.Plugins,
        "installations",
        QueryKeys.All,
      ];
      server.use(
        http.get("/api/translation/state", () => {
          reads++;
          return HttpResponse.json(
            firstRun({
              instances: [],
              available_engines: finished
                ? [OFFICIAL_OFFER, GOOGLE_OFFER]
                : [GOOGLE_OFFER],
            }),
          );
        }),
        http.get("/api/plugins/installations", async () => {
          if (returning) await installations.promise;
          return HttpResponse.json({
            data: finished
              ? []
              : [
                  {
                    provider_id: "ai_translator",
                    kind: "translator",
                    state: "removed",
                    active_version: "2.2.0",
                    needs_restart: false,
                  },
                ],
          });
        }),
      );

      const { unmount } = customRender(<TranslationStep onNext={onNext} />);
      expect(
        await screen.findByRole("button", {
          name: "Install AI Subtitle Translator",
        }),
      ).toBeDisabled();
      await waitFor(() =>
        expect(queryClient.getQueryState(installationsKey)?.status).toBe(
          "success",
        ),
      );
      expect(reads).toBe(1);
      unmount();
      returning = true;
      finished = !finishesDuringEntry;

      customRender(<TranslationStep onNext={onNext} />);
      try {
        // The cached draining row remains visible to the client until the fresh installation reply.
        await waitFor(() =>
          expect(
            queryClient.getQueryState(stateKey)?.dataUpdateCount,
          ).toBeGreaterThanOrEqual(2),
        );
        if (!finishesDuringEntry) {
          await waitFor(() =>
            expect(
              screen.getByRole("button", {
                name: "Install AI Subtitle Translator",
              }),
            ).toBeEnabled(),
          );
        }
      } finally {
        finished = true;
        installations.resolve();
      }
      await waitFor(() =>
        expect(
          screen.getByRole("button", {
            name: "Install AI Subtitle Translator",
          }),
        ).toBeEnabled(),
      );
      await waitFor(() =>
        expect(queryClient.getQueryData(installationsKey)).toEqual([]),
      );
      expect(reads).toBe(expectedReads);
    },
  );

  it.each([
    [{ state: "staged", active_version: null }, "It is installed. Restart"],
    [
      { state: "staged", active_version: "1.0.0" },
      "An update is staged. Restart",
    ],
    [
      { state: "removed", active_version: "1.0.0" },
      "It is being removed. Restart",
    ],
  ])(
    "words the restart by what it will do to an offered engine: %j",
    async (row, line) => {
      serve(firstRun({ instances: [], available_engines: [GOOGLE_OFFER] }));
      server.use(
        http.get("/api/plugins/installations", () =>
          HttpResponse.json({
            data: [
              {
                provider_id: "google_translate",
                kind: "translator",
                needs_restart: true,
                ...row,
              },
            ],
          }),
        ),
      );
      customRender(<TranslationStep onNext={onNext} />);
      expect(await screen.findByText(new RegExp(line))).toBeVisible();
    },
  );

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

  it("keeps the catalog's Google Translate install action when saved engine data remains", async () => {
    serve(
      firstRun({
        instances: [
          aiEngine(),
          { ...second, plugin: null, schema: null, configured: false },
        ],
      }),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(
      await screen.findByRole("button", { name: "Install Google Translate" }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Use Google Translate" }),
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

  it("says translation is set up when an engine is selected, and offers no key form that would add a second back end", async () => {
    serve(
      firstRun({
        engine: 1,
        instances: [
          aiEngine({ configured: true, connections: [connection(1)] }),
        ],
      }),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} />);

    expect(await screen.findByText("Translation is set up")).toBeVisible();
    expect(
      screen.getByText(/AI Subtitle Translator is selected and ready\./),
    ).toBeVisible();
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Start translating" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Continue without translation" }),
    ).not.toBeInTheDocument();

    // Keeping the configuration already present writes nothing.
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onNext).toHaveBeenCalledWith(NOTHING_WRITTEN);
    expect(calls).toEqual([]);
  });

  it("does not show the key form again once a start has saved a back end, whatever happened after", async () => {
    // A key saved while no engine got selected: typing it again would add a second back end.
    serve(
      firstRun({
        engine: null,
        instances: [aiEngine({ connections: [connection(1)] })],
      }),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(await screen.findByText("Translation is set up")).toBeVisible();
    expect(
      screen.getByText(/no engine is selected, so nothing translates yet/),
    ).toBeVisible();
    expect(screen.queryByLabelText("OpenRouter key")).not.toBeInTheDocument();
  });

  it("explains a selected engine that cannot take a job yet", async () => {
    serve(
      firstRun({
        engine: 1,
        instances: [
          aiEngine({ connections: [connection(1, { enabled: false })] }),
        ],
      }),
    );
    customRender(<TranslationStep onNext={onNext} />);

    expect(await screen.findByText("Translation is set up")).toBeVisible();
    expect(screen.getAllByText("No back end is on").length).toBeGreaterThan(0);
  });

  it("records a saved back end before follow-up reads fail and Continue writes nothing", async () => {
    const initial = firstRun();
    serve(initial);
    let created = false;
    const followup = Promise.withResolvers<void>();
    const followupRead = vi.fn();
    const onWritten = vi.fn();
    server.use(
      http.get("/api/translation/state", async () => {
        if (!created) return HttpResponse.json(initial);
        followupRead();
        await followup.promise;
        return new HttpResponse(null, { status: 500 });
      }),
      http.post(
        "/api/translation/engines/:id/connections",
        async ({ request }) => {
          record("post", "connection", await request.json());
          created = true;
          return HttpResponse.json(
            { connection: connection(3) },
            { status: 201 },
          );
        },
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslationStep onNext={onNext} onWritten={onWritten} />);

    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    try {
      await waitFor(() => expect(followupRead).toHaveBeenCalled());
      // The create is confirmed even though none of its follow-up reads has answered.
      expect(onWritten).toHaveBeenCalledOnce();
      expect(onNext).not.toHaveBeenCalled();
    } finally {
      followup.resolve();
    }

    expect(
      await screen.findByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    expect(queryClient.getQueryData([QueryKeys.Translation, "state"])).toEqual(
      initial,
    );
    const continueStep = screen.getByRole("button", {
      name: "Continue without translation",
    });
    await waitFor(() => expect(continueStep).toBeEnabled());
    expect(screen.getByLabelText("OpenRouter key")).toHaveValue("");
    await user.click(continueStep);

    expect(onNext).toHaveBeenCalledTimes(1);
    expect(onNext).toHaveBeenCalledWith(NOTHING_WRITTEN);
    expect(onWritten).toHaveBeenCalledOnce();
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
    ]);
  });

  it("keeps the key form and the reason when a start saved the back end and could not select the engine", async () => {
    // The back end exists after the first request, so the state says a back end is saved: the form still
    // has to stay up with what went wrong and the way to try again.
    let saved = false;
    serve(firstRun());
    server.use(
      http.get("/api/translation/state", () =>
        HttpResponse.json(
          saved
            ? firstRun({
                instances: [aiEngine({ connections: [connection(3)] })],
              })
            : firstRun(),
        ),
      ),
      http.post("/api/translation/engines/:id/connections", () => {
        saved = true;
        return HttpResponse.json(
          { connection: connection(3) },
          { status: 201 },
        );
      }),
      http.put(
        "/api/translation/pointers",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    const onWritten = vi.fn();
    customRender(<TranslationStep onNext={onNext} onWritten={onWritten} />);

    await user.type(await screen.findByLabelText("OpenRouter key"), SECRET);
    await user.click(screen.getByRole("button", { name: "Start translating" }));

    expect(
      await screen.findByText(
        /Your key is saved, but the engine could not be switched on/,
      ),
    ).toBeVisible();
    expect(screen.queryByText("Translation is set up")).not.toBeInTheDocument();
    // The shell already recorded the confirmed create; Continue adds no write.
    expect(onWritten).toHaveBeenCalledOnce();
    await user.click(screen.getByRole("button", { name: "Continue" }));
    expect(onNext).toHaveBeenCalledWith(NOTHING_WRITTEN);
    onNext.mockClear();
    // The key is stored; trying again only selects the engine.
    expect(
      screen.getByRole("button", { name: "Switch the engine on" }),
    ).toBeVisible();
    expect(onNext).not.toHaveBeenCalled();
  });
});
