/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type {
  PluginCatalog,
  PluginInstallation,
  PluginJob,
} from "@/apis/raw/plugins";
import {
  installSummary,
  quietDelta,
  updateDelta,
} from "@/components/plugin-consent/fixtures";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import {
  aiTranslator,
  catalog,
  communitySource,
  flaresolverr,
  googleTranslate,
  installation,
  installedBackend,
  installedTranslator,
  jobs as fixtureJobs,
  lookalike,
  officialSource,
  opencodeBackend,
  subdl,
  translatorWithBackend,
} from "./fixtures";
import PluginHubView from ".";

let calls: Array<{ method: string; path: string; body: unknown }>;
let current: PluginCatalog;
let installed: PluginInstallation[];
let jobs: PluginJob[];
let sandbox: Record<string, unknown>;

const record = (method: string, path: string, body: unknown = null) => {
  calls.push({ method, path, body });
};

beforeEach(() => {
  queryClient.clear();
  calls = [];
  window.history.replaceState({}, "", "/");
  current = catalog([
    aiTranslator,
    googleTranslate,
    opencodeBackend,
    flaresolverr,
    subdl,
  ]);
  installed = [];
  jobs = fixtureJobs;
  sandbox = {
    workers_sandboxed: true,
    network: "enforced",
    filesystem: "declared",
    label: "Sandboxed (Landlock ABI 4)",
    pending: false,
  };
  server.use(
    http.get("/api/plugins/catalog", () => HttpResponse.json(current)),
    http.get("/api/plugins/installations", () =>
      HttpResponse.json({ data: installed }),
    ),
    http.get("/api/plugins/jobs", () => HttpResponse.json({ data: jobs })),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { sandbox } }),
    ),
    http.get("/api/system/settings", () => HttpResponse.json({ general: {} })),
    http.get(
      "/api/translation/state",
      () => new HttpResponse(null, { status: 404 }),
    ),
    http.get("/api/plugins/catalog/consent", ({ request }) => {
      record(
        "get",
        "install-consent",
        Object.fromEntries(new URL(request.url).searchParams),
      );
      return HttpResponse.json(installSummary);
    }),
    http.post("/api/plugins/installations", async ({ request }) => {
      record("post", "install", await request.json());
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
    http.get("/api/plugins/updates/consent", () => {
      record("get", "update-consent");
      return HttpResponse.json(updateDelta);
    }),
    http.get("/api/plugins/installations/:id", ({ params }) =>
      HttpResponse.json(
        installed.find((item) => item.provider_id === params.id),
      ),
    ),
    http.post("/api/plugins/updates/apply", async ({ request }) => {
      record("post", "apply", await request.json());
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
  );
});

const showPage = (search = "") => {
  window.history.replaceState({}, "", `/${search}`);
  customRender(<PluginHubView />);
};

/** The dialog with this name, once it has finished fading in. */
const dialogNamed = async (name: string) => {
  const dialog = await screen.findByRole("dialog", { name });
  await waitFor(() => expect(dialog).toBeVisible());
  return dialog;
};

const card = async (name: string) => {
  const title = await screen.findByText(name);
  return title;
};

describe("the tabs", () => {
  it("lists the catalog's plugins of the kinds it shows, and no subtitle provider, on the Marketplace tab", async () => {
    showPage();
    expect(
      await screen.findByRole("tab", { name: "Marketplace" }),
    ).toHaveAttribute("aria-selected", "true");
    expect(await card("AI Subtitle Translator")).toBeVisible();
    expect(screen.getByText("Google Translate")).toBeVisible();
    expect(screen.getByText("Opencode back end")).toBeVisible();
    expect(screen.getByText("FlareSolverr")).toBeVisible();
    // The Subtitle Hub's, not this page's.
    expect(screen.queryByText("SubDL")).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Install AI Subtitle Translator" }),
    ).toBeEnabled();
  });

  it("lists what is installed on the Installed tab, with who needs what", async () => {
    installed = [translatorWithBackend, installedBackend];
    showPage("?tab=installed");
    expect(await card("AI Subtitle Translator")).toBeVisible();
    // The back end extends the translator, so the translator says it is needed.
    const dependencies = within(
      screen.getAllByRole("list", { name: "Dependencies" })[0],
    );
    expect(dependencies.getByText(/Opencode back end/)).toBeVisible();
    // The translator has a newer release listed, so its status says that, and the back end is active.
    expect(screen.getByText("Update available")).toBeVisible();
    expect(screen.getByText("Active")).toBeVisible();
    // Installed cards offer no install.
    expect(
      screen.queryByRole("button", { name: /^Install / }),
    ).not.toBeInTheDocument();
  });

  it("lists the updates on the Updates tab, with a count on the tab", async () => {
    installed = [installedTranslator];
    showPage();
    const tab = await screen.findByRole("tab", { name: /Updates/ });
    expect(await within(tab).findByLabelText("1 pending update")).toBeVisible();
    await userEvent.click(tab);
    expect(
      await screen.findByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    ).toBeVisible();
    expect(
      screen.getByRole("heading", { name: "Updates for your plugins" }),
    ).toBeVisible();
  });

  it("lists the jobs of its own plugins on the Activity tab, and not a provider's", async () => {
    showPage("?tab=activity");
    expect(
      await screen.findByText("Installed AI Subtitle Translator"),
    ).toBeVisible();
    expect(screen.getAllByText("Catalog refreshed").length).toBeGreaterThan(0);
    expect(screen.queryByText("Installed SubDL")).not.toBeInTheDocument();
  });
});

describe("the type filter", () => {
  it("narrows the cards to one kind, and keeps the choice in the address", async () => {
    const user = userEvent.setup();
    showPage();
    await card("AI Subtitle Translator");
    await user.click(
      screen.getByRole("radio", { name: "Translator back end" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByText("AI Subtitle Translator"),
      ).not.toBeInTheDocument(),
    );
    expect(screen.getByText("Opencode back end")).toBeVisible();
    expect(screen.queryByText("Google Translate")).not.toBeInTheDocument();
    expect(window.location.search).toBe("?type=translator_backend");
    await user.click(screen.getByRole("radio", { name: "Service" }));
    expect(await screen.findByText("FlareSolverr")).toBeVisible();
    expect(screen.queryByText("Opencode back end")).not.toBeInTheDocument();
    await user.click(screen.getByRole("radio", { name: "All" }));
    expect(await screen.findByText("Google Translate")).toBeVisible();
    expect(window.location.search).toBe("");
  });

  it("opens on the kind the address names", async () => {
    showPage("?type=service");
    expect(await screen.findByText("FlareSolverr")).toBeVisible();
    expect(screen.queryByText("Google Translate")).not.toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Service" })).toBeChecked();
  });
});

describe("the empty states", () => {
  it("says what a kind is for when the catalog has none of it", async () => {
    current = catalog([aiTranslator]);
    showPage("?type=service");
    expect(
      await screen.findByText("Nothing in the catalog for this"),
    ).toBeVisible();
    expect(
      screen.getByText(/Services are helpers other plugins can call/),
    ).toBeVisible();
  });

  it("says nothing is installed, and where to look, on the Installed tab", async () => {
    const user = userEvent.setup();
    showPage("?tab=installed");
    expect(await screen.findByText("Nothing installed yet")).toBeVisible();
    await user.click(
      screen.getByRole("button", { name: "Browse the Marketplace" }),
    );
    expect(
      await screen.findByRole("heading", { name: "Browse the catalog" }),
    ).toBeVisible();
  });

  it("says everything is current on the Updates tab, and nothing happened on the Activity tab", async () => {
    jobs = [];
    const user = userEvent.setup();
    showPage("?tab=updates");
    expect(await screen.findByText("Everything is up to date")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: /Activity/ }));
    expect(await screen.findByText("No plugin activity yet")).toBeVisible();
  });

  it("says no plugin matches a search", async () => {
    const user = userEvent.setup();
    showPage();
    await card("AI Subtitle Translator");
    await user.type(screen.getByLabelText("Search plugins"), "zzzz");
    expect(await screen.findByText("No matching plugins")).toBeVisible();
  });
});

describe("the Official mark", () => {
  it("is on the AI Subtitle Translator from the official source, and on nothing else", async () => {
    showPage();
    await card("AI Subtitle Translator");
    expect(screen.getAllByLabelText("Official")).toHaveLength(1);
    const translator = screen.getByText("AI Subtitle Translator");
    expect(within(translator).getByLabelText("Official")).toBeVisible();
  });

  it("is not given to the same id from another source, whatever its manifest says about itself", async () => {
    current = catalog([lookalike]);
    showPage();
    expect(await card("AI Subtitle Translator")).toBeVisible();
    expect(screen.queryByLabelText("Official")).not.toBeInTheDocument();
    // The source is Bazarr+'s record of it, not the manifest's own name for itself.
    expect(screen.getByText(/from Community/)).toBeVisible();
    expect(screen.queryByText(/from Official/)).not.toBeInTheDocument();
    // And it says why it cannot be installed.
    expect(
      screen.getByRole("button", {
        name: /unavailable: New kinds of plugin install only from the official catalog/,
      }),
    ).toBeVisible();
  });

  it("is not given when the source that is named official is not the official one", async () => {
    current = catalog(
      [{ ...aiTranslator, source: "official" }],
      [{ ...officialSource, official: false }, communitySource],
    );
    showPage();
    await card("AI Subtitle Translator");
    expect(screen.queryByLabelText("Official")).not.toBeInTheDocument();
  });

  it("is on the installed card only while the install is bound to the official source", async () => {
    installed = [
      installedTranslator,
      {
        ...installedBackend,
        provider_id: "ai_translator",
        name: "Impostor",
        source_id: "community",
        source_name: "Community",
      },
    ];
    showPage("?tab=installed");
    await card("AI Subtitle Translator");
    expect(screen.getAllByLabelText("Official")).toHaveLength(1);
  });
});

describe("what the cards say about this host", () => {
  it("marks a permission enforced only where the sandbox probe says so", async () => {
    showPage();
    await card("AI Subtitle Translator");
    expect(
      screen.getAllByText("Network access: enforced").length,
    ).toBeGreaterThan(0);
    expect(
      screen.getByText("Keep its own data folder: declared"),
    ).toBeVisible();
    // The workers are sandboxed here, so no card says they are not.
    expect(
      screen.queryByText("Not sandboxed on this host"),
    ).not.toBeInTheDocument();
  });

  it("calls everything declared, in neutral words, on a host that cannot sandbox", async () => {
    sandbox = {
      workers_sandboxed: false,
      network: "declared",
      filesystem: "declared",
      label: "Not sandboxed on this host",
      pending: false,
    };
    showPage();
    await card("AI Subtitle Translator");
    expect(screen.queryByText(/: enforced/)).not.toBeInTheDocument();
    expect(
      screen.getAllByText("Not sandboxed on this host").length,
    ).toBeGreaterThan(0);
  });
});

describe("what an installed plugin asks the host for", () => {
  it("lists each capability as ready or not, with the reason, on the Installed tab", async () => {
    installed = [
      installation(
        "ai_translator",
        "translator",
        { requires: ["flaresolverr.v1", "host.captcha.v1"] },
        {
          name: "AI Subtitle Translator",
          capabilities: [
            {
              capability: "flaresolverr.v1",
              source: "none",
              ready: false,
              reason: "No FlareSolverr address is set.",
            },
            {
              capability: "host.captcha.v1",
              source: "host",
              ready: true,
              reason: "",
            },
          ],
        },
      ),
    ];
    showPage("?tab=installed");
    const list = await screen.findByRole("list", { name: "Host capabilities" });
    const [solver, captcha] = within(list).getAllByRole("listitem");
    expect(within(solver).getByText("Not ready")).toBeVisible();
    expect(
      within(solver).getByText("No FlareSolverr address is set."),
    ).toBeVisible();
    expect(within(captcha).getByText("Ready")).toBeVisible();
  });

  it("shows no capability list for a plugin that needs nothing", async () => {
    installed = [installedTranslator];
    showPage("?tab=installed");
    await card("AI Subtitle Translator");
    expect(
      screen.queryByRole("list", { name: "Host capabilities" }),
    ).not.toBeInTheDocument();
  });
});

describe("the official source's standing", () => {
  it("says when the catalog was checked and how it is signed", async () => {
    showPage();
    const band = await screen.findByRole("group", {
      name: "Official catalog status",
    });
    expect(within(band).getByText("Checked 5m ago")).toBeVisible();
    expect(within(band).getByText("Signed catalog")).toBeVisible();
    expect(within(band).getByText("beta")).toBeVisible();
  });

  it("warns when the catalog has expired, and says running plugins are untouched", async () => {
    current = catalog(
      [aiTranslator],
      [
        { ...officialSource, catalog_expires_at: "2020-01-01T00:00:00Z" },
        communitySource,
      ],
    );
    showPage();
    expect(
      await screen.findByText("The official catalog has expired"),
    ).toBeVisible();
    expect(
      screen.getByText(/Plugins that are running are not touched/),
    ).toBeVisible();
  });

  it("says when what it shows is the snapshot that shipped with the release", async () => {
    current = catalog(
      [aiTranslator],
      [{ ...officialSource, catalog_status: "bundled" }, communitySource],
    );
    showPage();
    expect(await screen.findByText("Bundled snapshot")).toBeVisible();
  });
});

describe("installing and updating from a card", () => {
  it("opens the consent dialog, sends nothing until it is confirmed, and sends the digest with the install", async () => {
    const user = userEvent.setup();
    showPage();
    await user.click(
      await screen.findByRole("button", {
        name: "Install AI Subtitle Translator",
      }),
    );
    const dialog = await dialogNamed("Install AI Subtitle Translator?");
    const confirm = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(confirm).toBeEnabled());
    expect(calls.map((call) => call.path)).toEqual(["install-consent"]);
    expect(calls[0].body).toEqual({
      source: "official",
      id: "ai_translator",
      version: "2.0.0",
    });
    await user.click(confirm);
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "install-consent",
        "install",
      ]),
    );
    expect(calls[1].body).toEqual({
      source: "official",
      id: "ai_translator",
      version: "2.0.0",
      consent: "sha256:aaaa",
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
  });

  it("shows the server's refusal in the dialog", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.json(
          "This kind of plugin needs your consent before it is installed.",
          { status: 400 },
        ),
      ),
    );
    const user = userEvent.setup();
    showPage();
    await user.click(
      await screen.findByRole("button", { name: "Install Google Translate" }),
    );
    const dialog = await dialogNamed("Install Google Translate?");
    const confirm = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    expect(
      await within(dialog).findByText(
        "This kind of plugin needs your consent before it is installed.",
      ),
    ).toBeVisible();
  });

  it("asks again for an update that adds something, and goes ahead with one that does not", async () => {
    installed = [installedTranslator];
    const user = userEvent.setup();
    showPage("?tab=updates");
    await user.click(
      await screen.findByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    );
    const dialog = await dialogNamed("Update AI Subtitle Translator?");
    expect(
      await within(dialog).findByRole("group", { name: "What changes" }),
    ).toBeVisible();
    expect(calls.map((call) => call.path)).not.toContain("apply");
    await user.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );

    server.use(
      http.get("/api/plugins/updates/consent", () => {
        record("get", "update-consent");
        return HttpResponse.json(quietDelta);
      }),
    );
    await user.click(
      screen.getByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    );
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toContain("apply"),
    );
    expect(calls.findLast((call) => call.path === "apply")?.body).toEqual({
      id: "ai_translator",
      consent: "sha256:quiet",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("a page that cannot load", () => {
  it("says so, with a way to try again", async () => {
    server.use(
      http.get(
        "/api/plugins/catalog",
        () => new HttpResponse(null, { status: 500 }),
      ),
    );
    showPage();
    expect(
      await screen.findByText("The Plugin Hub could not be loaded"),
    ).toBeVisible();
    expect(screen.getByRole("button", { name: "Try again" })).toBeVisible();
    expect(screen.queryByRole("tab")).not.toBeInTheDocument();
  });
});

describe("a first install that failed", () => {
  const failed = installation(
    "ai_translator",
    "translator",
    {},
    {
      name: "AI Subtitle Translator",
      state: "failed",
      active_version: null,
      last_error: "Your proxy blocked the download.",
    },
  );

  it("is not shown as installed, says why, and offers the install again, on both tabs", async () => {
    installed = [failed];
    const user = userEvent.setup();
    showPage();
    expect(
      await screen.findByRole("button", {
        name: "Install AI Subtitle Translator",
      }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("button", { name: "Installed" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Failed")).toBeVisible();
    expect(screen.getByText("Your proxy blocked the download.")).toBeVisible();
    await user.click(screen.getByRole("tab", { name: "Installed" }));
    expect(
      await screen.findByRole("button", {
        name: "Install AI Subtitle Translator",
      }),
    ).toBeEnabled();
  });

  it("goes through the consent dialog and sends the digest when it is tried again", async () => {
    installed = [failed];
    const user = userEvent.setup();
    showPage("?tab=installed");
    await user.click(
      await screen.findByRole("button", {
        name: "Install AI Subtitle Translator",
      }),
    );
    const dialog = await dialogNamed("Install AI Subtitle Translator?");
    const confirm = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(confirm).toBeEnabled());
    await user.click(confirm);
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toContain("install"),
    );
    expect(calls.at(-1)?.body).toEqual({
      source: "official",
      id: "ai_translator",
      version: "2.0.0",
      consent: "sha256:aaaa",
    });
  });

  it("does not make the plugin it was meant to extend look needed", async () => {
    installed = [
      installedTranslator,
      { ...installedBackend, state: "failed", active_version: null },
    ];
    showPage("?tab=installed");
    await card("AI Subtitle Translator");
    expect(screen.queryByText(/Needed by/)).not.toBeInTheDocument();
  });
});

describe("the Updates tab under a type filter", () => {
  it("says there are none for this type, and that others have some", async () => {
    installed = [installedTranslator];
    showPage("?tab=updates&type=service");
    expect(await screen.findByText("No updates for this type")).toBeVisible();
    expect(
      screen.getByText(/1 update is waiting for other types/),
    ).toBeVisible();
  });

  it("holds the other Update buttons while one update goes ahead on its own", async () => {
    installed = [
      installedTranslator,
      installation(
        "google_translate",
        "translator",
        {},
        { name: "Google Translate" },
      ),
    ];
    current = catalog([aiTranslator, { ...googleTranslate, version: "1.1.0" }]);
    let release: () => void = () => undefined;
    const held = new Promise<void>((resolve) => (release = resolve));
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json(quietDelta),
      ),
      http.post("/api/plugins/updates/apply", async () => {
        await held;
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    showPage("?tab=updates");
    await user.click(
      await screen.findByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Update Google Translate to 1.1.0",
        }),
      ).toBeDisabled(),
    );
    release();
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: "Update Google Translate to 1.1.0",
        }),
      ).toBeEnabled(),
    );
  });
});

describe("updates waiting for a restart, and the restart prompt", () => {
  const staged: PluginInstallation = {
    ...installedTranslator,
    state: "staged",
    active_version: "1.0.0",
    staged_version: "2.0.0",
    needs_restart: true,
  };

  it("counts a staged update with the ones on offer, and keeps it on the Updates tab until the restart", async () => {
    installed = [staged];
    showPage();
    const tab = await screen.findByRole("tab", { name: /Updates/ });
    expect(await within(tab).findByLabelText("1 pending update")).toBeVisible();
    await userEvent.click(tab);
    expect(
      await screen.findByRole("heading", { name: "Awaiting restart" }),
    ).toBeVisible();
    expect(screen.getAllByText("Awaiting restart").length).toBeGreaterThan(1);
    // Nothing is offered on top of it, and the tab does not claim everything is current.
    expect(
      screen.queryByRole("button", { name: /^Update / }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Everything is up to date"),
    ).not.toBeInTheDocument();
  });

  it("raises the restart prompt from the field alone, and drops it when the field goes", async () => {
    installed = [{ ...installedTranslator, needs_restart: true }];
    showPage("?tab=installed");
    const banner = await screen.findByRole("alert");
    expect(banner).toHaveTextContent(
      "1 plugin change is waiting for a restart.",
    );
    expect(
      within(banner).getByRole("button", { name: "Restart Bazarr now" }),
    ).toBeEnabled();
    installed = [{ ...installedTranslator, needs_restart: false }];
    await queryClient.invalidateQueries();
    await waitFor(() =>
      expect(screen.queryByRole("alert")).not.toBeInTheDocument(),
    );
  });

  it("lets a revoked running release be updated or removed before the restart that stops it", async () => {
    installed = [
      {
        ...installedTranslator,
        needs_restart: true,
        revoked: true,
        revoked_reason:
          "Version 1.0.0 was revoked. Restart Bazarr+ to stop it.",
      },
    ];
    showPage("?tab=updates");
    // The restart prompt stays, and so do the way out of it: the update and the removal.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "waiting for a restart",
    );
    expect(
      await screen.findByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    ).toBeEnabled();
    expect(screen.queryByText("Awaiting restart")).not.toBeInTheDocument();
    expect(screen.getByText(/Restart Bazarr\+ to stop it/)).toBeVisible();
    await userEvent.click(
      screen.getByRole("button", {
        name: "AI Subtitle Translator actions",
      }),
    );
    expect(
      screen.queryByText("Restart Bazarr+ to finish the change in progress."),
    ).not.toBeInTheDocument();
  });

  it("still waits for the restart when the release is revoked and an update is already staged", async () => {
    installed = [{ ...staged, revoked: true, revoked_reason: "Revoked." }];
    showPage("?tab=installed");
    await card("AI Subtitle Translator");
    expect(screen.getAllByText("Awaiting restart").length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", { name: /^Update / }),
    ).not.toBeInTheDocument();
  });

  it("shows no restart prompt for a change the server does not say needs one, whatever else the row holds", async () => {
    installed = [
      {
        ...installedTranslator,
        state: "staged",
        active_version: null,
        staged_version: "2.0.0",
        pending_restart: true,
      } as unknown as PluginInstallation,
    ];
    showPage("?tab=installed");
    await card("AI Subtitle Translator");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByText("Awaiting restart")).not.toBeInTheDocument();
  });
});

describe("the Activity tab's failure copy", () => {
  it("says what went wrong and what to do next, never the raw error", async () => {
    jobs = [
      {
        id: "9",
        action: "install",
        state: "failed",
        target_kind: "provider",
        target_id: "ai_translator",
        target_name: "AI Subtitle Translator",
        message: "Install failed",
        error: "pip install failed: proxy tunnel returned 407",
        created_at: "2026-11-16T12:00:00Z",
      },
    ];
    const user = userEvent.setup();
    showPage("?tab=activity");
    await user.click(await screen.findByText("Install failed"));
    expect(
      await screen.findByText("Your proxy blocked the download."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Check the proxy setting in Settings > General."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/pip install failed/)).not.toBeInTheDocument();
  });
});

describe("the two states an update can wait in", () => {
  it("marks an update on offer that needs the person's consent, and none that does not", async () => {
    installed = [installedTranslator];
    showPage("?tab=updates");
    expect(await screen.findByText("Needs your consent")).toBeVisible();
    expect(
      screen.getByText(/1 available, 1 that needs your consent/),
    ).toBeVisible();
    expect(screen.queryByText(/awaiting restart/)).not.toBeInTheDocument();
  });

  it("does not mark an update whose summary adds nothing", async () => {
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json(quietDelta),
      ),
    );
    installed = [installedTranslator];
    showPage("?tab=updates");
    expect(
      await screen.findByRole("button", {
        name: "Update AI Subtitle Translator to 2.0.0",
      }),
    ).toBeVisible();
    expect(screen.queryByText("Needs your consent")).not.toBeInTheDocument();
    expect(screen.getByText(/^1 available\./)).toBeVisible();
  });

  it("keeps a staged update as awaiting restart, never as needing consent", async () => {
    installed = [
      {
        ...installedTranslator,
        state: "staged",
        staged_version: "2.0.0",
        needs_restart: true,
      },
    ];
    showPage("?tab=updates");
    expect(
      await screen.findByRole("heading", { name: "Awaiting restart" }),
    ).toBeVisible();
    expect(screen.queryByText("Needs your consent")).not.toBeInTheDocument();
    expect(screen.getByText(/0 available, 1 awaiting restart/)).toBeVisible();
  });
});

describe("a plugin that is being removed while Bazarr+ runs", () => {
  it("says Removing on its card, offers no update and disables every action", async () => {
    installed = [
      {
        ...installedTranslator,
        state: "removed",
        needs_restart: false,
      },
    ];
    current = catalog([aiTranslator]);
    showPage("?tab=marketplace");

    expect((await screen.findAllByText("Removing")).length).toBeGreaterThan(0);
    expect(
      screen.queryByRole("button", {
        name: /Update AI Subtitle Translator/,
      }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Installed" }),
    ).not.toBeInTheDocument();
    await userEvent.click(
      await screen.findByRole("button", {
        name: "AI Subtitle Translator actions",
      }),
    );
    for (const name of [/Turn off/, /Repair/, /Remove/]) {
      expect(
        await screen.findByRole("menuitem", { name, hidden: true }),
      ).toHaveAttribute("aria-disabled", "true");
    }
  });
});
