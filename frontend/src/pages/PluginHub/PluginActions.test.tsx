/* eslint-disable camelcase -- transport names. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import type { PluginInstallation, PluginJob } from "@/apis/raw/plugins";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { PluginMenu } from "./components/PluginMenu";
import {
  aiTranslator,
  catalog,
  installation,
  installedBackend,
  installedTranslator,
  opencodeBackend,
} from "./fixtures";
import { installedRows } from "./model";
import PluginHubView from ".";

const notified = vi.hoisted(() => vi.fn());
vi.mock("@mantine/notifications", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@mantine/notifications")>()),
  showNotification: notified,
}));

let calls: Array<{ method: string; path: string; body: unknown }>;
let installed: PluginInstallation[];
let jobs: PluginJob[];
let auth: Record<string, unknown> | undefined;
let logAnswer: { lines: string[]; last_error: string | null };
let translationState: unknown;

const record = (method: string, path: string, body: unknown = null) => {
  calls.push({ method, path, body });
};

// The catalog lists the same releases that are installed, so no card offers an update.
const sameReleases = () =>
  catalog([
    { ...aiTranslator, version: "1.0.0" },
    { ...opencodeBackend, version: "1.0.0" },
  ]);

beforeEach(() => {
  notified.mockClear();
  queryClient.clear();
  forgetPasswordConfirmation();
  calls = [];
  auth = undefined;
  jobs = [];
  logAnswer = { lines: [], last_error: null };
  translationState = null;
  installed = [installedTranslator, installedBackend];
  window.history.replaceState({}, "", "/?tab=installed");
  server.use(
    http.get("/api/plugins/catalog", () => HttpResponse.json(sameReleases())),
    http.get("/api/plugins/installations", () =>
      HttpResponse.json({ data: installed }),
    ),
    http.get("/api/plugins/jobs", () => HttpResponse.json({ data: jobs })),
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: { sandbox: { workers_sandboxed: true, network: "enforced" } },
      }),
    ),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: {}, auth }),
    ),
    http.get("/api/translation/state", () =>
      translationState
        ? HttpResponse.json(translationState)
        : new HttpResponse(null, { status: 404 }),
    ),
    http.get("/api/plugins/installations/:id/log", () =>
      HttpResponse.json(logAnswer),
    ),
    http.patch(
      "/api/plugins/installations/:id",
      async ({ request, params }) => {
        record("patch", String(params.id), await request.json());
        return HttpResponse.json({});
      },
    ),
    http.post("/api/plugins/installations/:id/repair", ({ params }) => {
      record("post", `repair ${String(params.id)}`);
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
    http.delete("/api/plugins/installations/:id", ({ params, request }) => {
      record(
        "delete",
        String(params.id),
        Object.fromEntries(new URL(request.url).searchParams),
      );
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
    http.post("/api/plugins/installations/:id/review/dismiss", ({ params }) => {
      record("post", `dismiss ${String(params.id)}`);
      installed = installed.map((item) =>
        item.provider_id === params.id
          ? { ...item, review_pending: false }
          : item,
      );
      return HttpResponse.json({ review_pending: false });
    }),
  );
});

const showPage = () => customRender(<PluginHubView />);

/** Visible once the dialog or menu has finished fading in. */
const shown = async (element: HTMLElement) => {
  await waitFor(() => expect(element).toBeVisible());
  return element;
};

const menuOf = async (name: string) => {
  const user = userEvent.setup();
  await user.click(
    await screen.findByRole("button", { name: `${name} actions` }),
  );
  return user;
};

describe("the actions of an installed plugin", () => {
  it("turns a plugin off", async () => {
    installed = [installedBackend];
    showPage();
    const user = await menuOf("Opencode back end");
    await user.click(await screen.findByRole("menuitem", { name: "Turn off" }));
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "patch", path: "opencode_backend", body: { enabled: false } },
      ]),
    );
  });

  it("turns a plugin that is off back on, and says it is off meanwhile", async () => {
    installed = [{ ...installedBackend, enabled: false }];
    showPage();
    expect(await screen.findByText("Off")).toBeVisible();
    const user = await menuOf("Opencode back end");
    await user.click(await screen.findByRole("menuitem", { name: "Turn on" }));
    await waitFor(() =>
      expect(calls).toEqual([
        { method: "patch", path: "opencode_backend", body: { enabled: true } },
      ]),
    );
  });

  it("will not turn a plugin off while one that needs it is on, and says why", async () => {
    showPage();
    await menuOf("AI Subtitle Translator");
    const item = await screen.findByRole("menuitem", { name: /Turn off/ });
    expect(item).toHaveAttribute("aria-disabled", "true");
    // The reason is in the item's own text, so a screen reader reads it with the name.
    expect(item).toHaveTextContent(
      "Opencode back end needs it. Turn that off first.",
    );
  });

  it("repairs a plugin, asking for the password first where a login is set up", async () => {
    auth = { type: "form", username: "admin", password: "***" };
    server.use(
      http.post("/api/system/account/confirm", () => {
        record("post", "confirm");
        return HttpResponse.json({});
      }),
    );
    installed = [{ ...installedBackend, needs_repair: true }];
    showPage();
    const user = await menuOf("Opencode back end");
    await user.click(await screen.findByRole("menuitem", { name: "Repair" }));
    await shown(await screen.findByText("Confirm your password"));
    // Nothing starts before the password is given.
    expect(calls).toEqual([]);
    await user.type(screen.getByLabelText("Password"), "hunter2");
    await user.click(screen.getByRole("button", { name: /^confirm$/i }));
    await waitFor(() =>
      expect(calls.map((call) => call.path)).toEqual([
        "confirm",
        "repair opencode_backend",
      ]),
    );
  });

  it("shows the plugin's log, with what the server kept of it", async () => {
    logAnswer = {
      lines: ["worker started", "Authorization: Bearer [hidden]"],
      last_error: null,
    };
    showPage();
    const user = await menuOf("AI Subtitle Translator");
    await user.click(await screen.findByRole("menuitem", { name: "View log" }));
    const log = await screen.findByLabelText("Recent output");
    expect(log).toHaveTextContent("worker started");
    expect(log).toHaveTextContent("Authorization: Bearer [hidden]");
  });

  it("says so when there is nothing in the log yet", async () => {
    showPage();
    const user = await menuOf("AI Subtitle Translator");
    await user.click(await screen.findByRole("menuitem", { name: "View log" }));
    await shown(await screen.findByText(/No output yet/));
  });
});

describe("removing a plugin", () => {
  it("refuses while another plugin needs it, and names that plugin", async () => {
    showPage();
    const user = await menuOf("AI Subtitle Translator");
    await user.click(await screen.findByRole("menuitem", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", {
      name: "Remove AI Subtitle Translator?",
    });
    await shown(
      within(dialog).getByText(
        /Opencode back end needs AI Subtitle Translator/,
      ),
    );
    expect(
      within(dialog).getByRole("button", { name: "Remove" }),
    ).toBeDisabled();
    expect(calls).toEqual([]);
  });

  const openRemove = async (name: string) => {
    const user = await menuOf(name);
    await user.click(await screen.findByRole("menuitem", { name: "Remove" }));
    const dialog = await screen.findByRole("dialog", {
      name: `Remove ${name}?`,
    });
    return { user, dialog };
  };

  it("keeps the plugin's data by default, and sends nothing before it is confirmed", async () => {
    installed = [installedBackend];
    showPage();
    const { user, dialog } = await openRemove("Opencode back end");
    expect(
      within(dialog).getByRole("radio", { name: /Keep its settings and data/ }),
    ).toBeChecked();
    expect(calls).toEqual([]);
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(calls).toEqual([
        {
          method: "delete",
          path: "opencode_backend",
          body: { keep_data: "true" },
        },
      ]),
    );
  });

  it("deletes the plugin's data when that is chosen", async () => {
    installed = [installedBackend];
    showPage();
    const { user, dialog } = await openRemove("Opencode back end");
    await user.click(
      within(dialog).getByRole("radio", {
        name: /Delete its settings and data/,
      }),
    );
    await user.click(within(dialog).getByRole("button", { name: "Remove" }));
    await waitFor(() =>
      expect(calls).toEqual([
        {
          method: "delete",
          path: "opencode_backend",
          body: { keep_data: "false" },
        },
      ]),
    );
  });

  it("says when it would leave the engine with no enabled connection", async () => {
    installed = [installedBackend];
    translationState = {
      enabled: true,
      engine: 1,
      fallback_engine: null,
      instances: [
        {
          id: 1,
          plugin_id: "ai_translator",
          name: "AI Subtitle Translator",
          enabled: true,
          connections: [
            { id: 5, plugin_id: "opencode_backend", enabled: true },
            { id: 6, plugin_id: "ai_translator", enabled: false },
          ],
        },
      ],
    };
    showPage();
    const user = await menuOf("Opencode back end");
    await user.click(await screen.findByRole("menuitem", { name: "Remove" }));
    await shown(
      await screen.findByText(
        "AI Subtitle Translator would have no enabled connection left, so it could not translate until you add one.",
      ),
    );
  });

  it("says the engine stays, and cannot translate, while its data is kept, and goes when the data is deleted", async () => {
    installed = [installedTranslator];
    translationState = {
      enabled: true,
      engine: 1,
      fallback_engine: null,
      instances: [
        {
          id: 1,
          plugin_id: "ai_translator",
          name: "AI Subtitle Translator",
          enabled: true,
          connections: [],
        },
      ],
    };
    showPage();
    const { user, dialog } = await openRemove("AI Subtitle Translator");
    expect(
      await within(dialog).findByText(/It stays there with its settings/),
    ).toBeInTheDocument();
    await user.click(
      within(dialog).getByRole("radio", {
        name: /Delete its settings and data/,
      }),
    );
    expect(
      await within(dialog).findByText(
        /Removing the plugin removes the engine and its connections/,
      ),
    ).toBeInTheDocument();
  });

  describe("where a login is set up", () => {
    beforeEach(() => {
      auth = { type: "form", username: "admin", password: "***" };
      server.use(
        http.post("/api/system/account/confirm", () => {
          record("post", "confirm");
          return HttpResponse.json({});
        }),
      );
    });

    it("asks for the password before anything is sent, for either choice about the data", async () => {
      installed = [installedBackend];
      showPage();
      const { user, dialog } = await openRemove("Opencode back end");
      await user.click(
        within(dialog).getByRole("radio", {
          name: /Delete its settings and data/,
        }),
      );
      await user.click(within(dialog).getByRole("button", { name: "Remove" }));
      await shown(await screen.findByText("Confirm your password"));
      expect(calls).toEqual([]);
      await user.type(screen.getByLabelText("Password"), "hunter2");
      await user.click(screen.getByRole("button", { name: /^confirm$/i }));
      await waitFor(() =>
        expect(calls.map((call) => call.path)).toEqual([
          "confirm",
          "opencode_backend",
        ]),
      );
      expect(calls.at(-1)?.body).toEqual({ keep_data: "false" });
    });

    it("does nothing and says nothing when the password question is closed", async () => {
      installed = [installedBackend];
      showPage();
      const { user, dialog } = await openRemove("Opencode back end");
      await user.click(within(dialog).getByRole("button", { name: "Remove" }));
      await shown(await screen.findByText("Confirm your password"));
      await user.click(
        within(
          screen.getByRole("dialog", { name: "Confirm your password" }),
        ).getByRole("button", { name: "Cancel" }),
      );
      await waitFor(() =>
        expect(
          screen.queryByText("Confirm your password"),
        ).not.toBeInTheDocument(),
      );
      expect(calls).toEqual([]);
      expect(notified).not.toHaveBeenCalled();
      // The removal dialog is still open, as it was, for another try.
      expect(
        screen.getByRole("dialog", { name: "Remove Opencode back end?" }),
      ).toBeInTheDocument();
    });

    it("does nothing and says nothing when a repair's password question is closed", async () => {
      installed = [{ ...installedBackend, needs_repair: true }];
      showPage();
      const user = await menuOf("Opencode back end");
      await user.click(await screen.findByRole("menuitem", { name: "Repair" }));
      await shown(await screen.findByText("Confirm your password"));
      await user.click(
        within(
          screen.getByRole("dialog", { name: "Confirm your password" }),
        ).getByRole("button", { name: "Cancel" }),
      );
      await waitFor(() =>
        expect(
          screen.queryByText("Confirm your password"),
        ).not.toBeInTheDocument(),
      );
      expect(calls).toEqual([]);
      expect(notified).not.toHaveBeenCalled();
    });
  });
});

describe("what a card says about the plugin", () => {
  it("says what the worker is doing, in text", async () => {
    installed = [
      {
        ...installedTranslator,
        runtime: {
          phase: "backing_off",
          in_flight: 0,
          retry_after: 8,
          last_error: null,
          starts: 2,
          crashes: 1,
          memory_bytes: 0,
        },
      },
    ];
    showPage();
    expect(
      await screen.findByText(
        /Backing off after a failure, trying again in 8 s/,
      ),
    ).toBeVisible();
  });

  it("says Rebuilding while a repair job runs", async () => {
    jobs = [
      {
        id: "9",
        action: "repair",
        state: "running",
        target_kind: "provider",
        target_id: "opencode_backend",
      },
    ];
    installed = [installedBackend];
    showPage();
    expect(await screen.findByText("Rebuilding")).toBeVisible();
    await menuOf("Opencode back end");
    expect(
      await screen.findByRole("menuitem", { name: /Repair/ }),
    ).toHaveAttribute("aria-disabled", "true");
  });

  it("says what needs attention, with the cause and the next step", async () => {
    installed = [
      {
        ...installedBackend,
        needs_repair: true,
        last_error: "Your proxy blocked the download.",
      },
    ];
    showPage();
    expect(await screen.findByText("Needs attention")).toBeVisible();
    expect(screen.getByText("Your proxy blocked the download.")).toBeVisible();
    expect(
      screen.getByText("Check the proxy setting in Settings > General."),
    ).toBeVisible();
  });
});

describe("the review notice", () => {
  const reviewed = installation(
    "ai_translator",
    "translator",
    {
      permissions: ["network"],
      endpoints: ["https://openrouter.ai:443"],
    },
    { name: "AI Subtitle Translator", review_pending: true },
  );

  it("shows what the plugin may do, and is gone for good once dismissed", async () => {
    installed = [reviewed];
    const user = userEvent.setup();
    showPage();
    const notice = await screen.findByRole("region", {
      name: "Review AI Subtitle Translator",
    });
    expect(within(notice).getByText(/Network access: enforced/)).toBeVisible();
    expect(within(notice).getByText(/openrouter\.ai/)).toBeVisible();
    await user.click(within(notice).getByRole("button", { name: "Dismiss" }));
    await waitFor(() =>
      expect(calls).toContainEqual({
        method: "post",
        path: "dismiss ai_translator",
        body: null,
      }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("region", { name: "Review AI Subtitle Translator" }),
      ).not.toBeInTheDocument(),
    );
    expect(
      calls.filter((call) => call.path.startsWith("dismiss")),
    ).toHaveLength(1);
  });

  it("is not shown for a plugin with nothing to review", async () => {
    installed = [installedTranslator];
    showPage();
    await screen.findByText("AI Subtitle Translator");
    expect(
      screen.queryByRole("region", { name: /Review/ }),
    ).not.toBeInTheDocument();
  });
});

describe("one change at a time", () => {
  it("disables the actions of a plugin while another one is being changed, and says why", async () => {
    const [row] = installedRows(undefined, [installedBackend], "all");
    const none = () => undefined;
    customRender(
      <PluginMenu
        row={row}
        enabledDependents={[]}
        busy={false}
        elsewhere
        onToggle={none}
        onRepair={none}
        onLog={none}
        onRemove={none}
      />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Opencode back end actions" }),
    );
    const [item, log] = await Promise.all([
      screen.findByRole("menuitem", { name: /Repair/ }),
      screen.findByRole("menuitem", { name: "View log" }),
    ]);
    expect(item).toHaveAttribute("aria-disabled", "true");
    expect(item).toHaveTextContent(
      "Another plugin is being changed. Wait for it to finish.",
    );
    // The log can always be read.
    expect(log).not.toHaveAttribute("aria-disabled");
  });

  it("returns focus to the card's actions button when the log is closed", async () => {
    showPage();
    const user = await menuOf("AI Subtitle Translator");
    await user.click(await screen.findByRole("menuitem", { name: "View log" }));
    await screen.findByLabelText("Loading the log").catch(() => undefined);
    await user.keyboard("{Escape}");
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "AI Subtitle Translator actions" }),
      ).toHaveFocus(),
    );
  });
});
