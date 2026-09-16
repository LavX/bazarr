import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { Text } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import App from "@/App";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { AllProviders } from "@/providers";
import { act, customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setOnlineStatus } from "@/utilities/event";
import { markWhatsNewSeen } from "@/utilities/whatsNew";
import { Check, Password, Text as TextField } from "./forms";
import Layout from "./Layout";
import LayoutModal from "./LayoutModal";

// An input that only stages its value when it is left, the way the AI Model
// field commits an OpenRouter routing shortcut.
function CommitOnBlurInput() {
  const { setValue } = useFormActions();

  return (
    <input
      aria-label="Commits on blur"
      onBlur={(e) =>
        setValue(e.currentTarget.value, "settings-general-instance_name")
      }
    />
  );
}

function StageChangeButton() {
  const { setValue } = useFormActions();

  return (
    <button
      type="button"
      onClick={() => setValue("changed", "settings-general-instance_name")}
    >
      Stage change
    </button>
  );
}

describe("Settings layout", () => {
  it.concurrent("should be able to render without issues", () => {
    customRender(
      <Layout name="Test Settings">
        <Text>Value</Text>
      </Layout>,
    );
  });

  it.concurrent(
    "save button should not be visible when no changes are staged",
    () => {
      customRender(
        <Layout name="Test Settings">
          <Text>Value</Text>
        </Layout>,
      );

      // The floating save button is hidden when totalStagedCount === 0
      expect(
        screen.queryByRole("button", { name: /save/i }),
      ).not.toBeInTheDocument();
    },
  );

  it.concurrent("renders children content", () => {
    customRender(
      <Layout name="Test Settings">
        <Text>Test Content</Text>
      </Layout>,
    );

    expect(screen.getByText("Test Content")).toBeInTheDocument();
  });

  it("can render a fluid content area", () => {
    customRender(
      <Layout name="Test Settings" fluid>
        <Text>Test Content</Text>
      </Layout>,
    );

    expect(screen.getByTestId("settings-layout-content")).toHaveStyle({
      maxWidth: "none",
      width: "100%",
    });
  });

  it("shows a readable pending-change count on the floating save button", async () => {
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
      </Layout>,
    );

    await userEvent.click(screen.getByRole("button", { name: "Stage change" }));

    await waitFor(() => {
      expect(
        screen.getByRole("button", { name: "Save 1 pending change" }),
      ).toBeInTheDocument();
    });
    expect(screen.getByLabelText("1 unsaved change")).toHaveTextContent("1");
  });

  it("commits the focused field before a keyboard save submits", async () => {
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
        <CommitOnBlurInput />
      </Layout>,
    );

    await user.click(screen.getByRole("button", { name: "Stage change" }));

    const input = screen.getByLabelText("Commits on blur");
    await user.click(input);
    await user.keyboard("typed-while-focused");
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "typed-while-focused",
    );
  });

  it("commits the focused field when Enter submits the form", async () => {
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <StageChangeButton />
        <CommitOnBlurInput />
      </Layout>,
    );

    await user.click(screen.getByRole("button", { name: "Stage change" }));

    const input = screen.getByLabelText("Commits on blur");
    await user.click(input);
    await user.keyboard("typed-then-enter{Enter}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "typed-then-enter",
    );
  });
});

describe("Settings layout keyboard save", () => {
  it("saves a blur-staged field that is the only change", async () => {
    // The staged count used to be read before the blur, so a field that stages only
    // when it is left was not counted yet when the keystroke arrived. The shortcut
    // then returned without submitting, and silently threw the typing away.
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <CommitOnBlurInput />
      </Layout>,
    );

    await user.click(screen.getByLabelText("Commits on blur"));
    await user.keyboard("only-change");
    await user.keyboard("{Control>}s{/Control}");

    await waitFor(() => {
      expect(submitted).toHaveLength(1);
    });
    expect(submitted[0]["settings-general-instance_name"]).toBe("only-change");
  });

  it("submits nothing when there is nothing to save", async () => {
    const submitted: LooseObject[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const form = await request.formData();
        submitted.push(Object.fromEntries(form.entries()));
        return HttpResponse.json({});
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <Text>Nothing staged</Text>
      </Layout>,
    );

    await user.keyboard("{Control>}s{/Control}");

    expect(await screen.findByText("Nothing staged")).toBeInTheDocument();
    expect(submitted).toHaveLength(0);
  });
});

it("submits a secret while the active development logger records only field names", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  try {
    customRender(
      <Layout name="Logging test">
        <CommitOnBlurInput />
      </Layout>,
    );
    await userEvent.type(
      screen.getByLabelText("Commits on blur"),
      "sentinel-layout-secret{Enter}",
    );
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]["settings-general-instance_name"]).toBe(
      "sentinel-layout-secret",
    );
    expect(log).toHaveBeenCalledWith("[info] submitting settings", [
      "settings-general-instance_name",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-layout-secret");
  } finally {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});

it("keeps modal submission values out of the active development logger", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  const close = vi.fn();
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  try {
    customRender(
      <LayoutModal callbackModal={close}>
        <Password label="API Key" settingKey="settings-silo-apikey" />
      </LayoutModal>,
    );
    await userEvent.type(
      screen.getByLabelText("API Key"),
      "sentinel-modal-secret",
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]["settings-silo-apikey"]).toBe("sentinel-modal-secret");
    expect(log).toHaveBeenCalledWith("[info] submitting settings", [
      "settings-silo-apikey",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-modal-secret");
    await waitFor(() => expect(close).toHaveBeenCalledWith(true));
  } finally {
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});

it("keeps Save and Leave values out of the active development logger", async () => {
  vi.stubEnv("MODE", "development");
  const log = vi.spyOn(console, "log").mockImplementation(() => undefined);
  const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
  const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
  const submitted: LooseObject[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(Object.fromEntries((await request.formData()).entries()));
      return HttpResponse.json({});
    }),
  );
  const router = createMemoryRouter([
    {
      path: "/",
      element: (
        <Layout name="Logging test">
          <Password label="API Key" settingKey="settings-emby-apikey" />
          <Link to="/away">Leave settings</Link>
        </Layout>
      ),
    },
    { path: "/away", element: <p>Destination</p> },
  ]);
  try {
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    await userEvent.type(
      screen.getByLabelText("API Key"),
      "sentinel-leave-secret",
    );
    await userEvent.click(screen.getByRole("link", { name: "Leave settings" }));
    await userEvent.click(
      await screen.findByRole("button", {
        name: "Save and leave",
      }),
    );
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(await screen.findByText("Destination")).toBeInTheDocument();
    expect(submitted[0]["settings-emby-apikey"]).toBe("sentinel-leave-secret");
    expect(log).toHaveBeenCalledWith("[info] save & leave", [
      "settings-emby-apikey",
    ]);
    expect(
      JSON.stringify([log.mock.calls, warn.mock.calls, error.mock.calls]),
    ).not.toContain("sentinel-leave-secret");
  } finally {
    router.dispose();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  }
});

describe("Settings layout refetch", () => {
  const persisted = () => ({
    general: { theme: "auto" },
    sonarr: { ssl: false, ip: "sonarr.internal" },
  });

  function serveSettings(body: () => LooseObject = persisted) {
    server.use(
      http.get("/api/system/settings", () => HttpResponse.json(body())),
    );
  }

  // Holds every further settings response open, so an edit can be staged while
  // a refresh is in flight. That is the window the race lives in: on the real
  // page the edit disappeared when the held response finally landed.
  function holdSettings(body: () => LooseObject = persisted) {
    let release!: () => void;
    let markReached!: () => void;
    const held = new Promise<void>((resolve) => (release = resolve));
    const reached = new Promise<void>((resolve) => (markReached = resolve));

    server.use(
      http.get("/api/system/settings", async () => {
        markReached();
        await held;
        return HttpResponse.json(body());
      }),
    );

    return { reached, release };
  }

  const stagedFields = (
    <Layout name="Test Settings">
      <Check label="Use SSL" settingKey="settings-sonarr-ssl" />
      <TextField label="Address" settingKey="settings-sonarr-ip" />
    </Layout>
  );

  async function waitForHydration() {
    await waitFor(() => {
      expect(screen.getByLabelText("Address")).toHaveValue("sonarr.internal");
    });
  }

  async function stageBothFields(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByLabelText("Use SSL"));
    await user.type(screen.getByLabelText("Address"), "-staged");

    expect(
      await screen.findByRole("button", { name: "Save 2 pending changes" }),
    ).toBeInTheDocument();
  }

  async function expectStillStaged() {
    await waitFor(() => {
      expect(queryClient.isFetching()).toBe(0);
    });

    expect(screen.getByLabelText("Use SSL")).toBeChecked();
    expect(screen.getByLabelText("Address")).toHaveValue(
      "sonarr.internal-staged",
    );
    expect(
      screen.getByRole("button", { name: "Save 2 pending changes" }),
    ).toBeInTheDocument();
  }

  function mountSettingsInApp() {
    // The wizard would otherwise cover the page on a fresh profile.
    markWhatsNewSeen(latestWhatsNewVersion);
    // The shell around the page has queries of its own, and the refresh the
    // socket triggers hits every one of them.
    server.use(
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    );
    serveSettings();

    const router = createMemoryRouter([
      {
        path: "/",
        element: <App />,
        children: [{ index: true, element: stagedFields }],
      },
    ]);

    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );

    return router;
  }

  it("keeps staged values through the startup refresh", async () => {
    const user = userEvent.setup();
    const router = mountSettingsInApp();

    try {
      await waitForHydration();

      // The socket reporting online for the first time refreshes every active
      // query, the settings this page is built from included.
      const settings = holdSettings();
      await act(async () => {
        setOnlineStatus(true);
      });
      await settings.reached;

      await stageBothFields(user);

      settings.release();
      await expectStillStaged();
    } finally {
      router.dispose();
    }
  });

  it("keeps staged values through a reconnect refresh", async () => {
    const user = userEvent.setup();
    const router = mountSettingsInApp();

    try {
      await waitForHydration();

      await act(async () => {
        setOnlineStatus(true);
      });
      await waitFor(() => {
        expect(queryClient.isFetching()).toBe(0);
      });

      const settings = holdSettings();
      await act(async () => {
        setOnlineStatus(false);
      });
      await act(async () => {
        setOnlineStatus(true);
      });
      await settings.reached;

      await stageBothFields(user);

      settings.release();
      await expectStillStaged();
    } finally {
      router.dispose();
    }
  });

  it("clears the form once a successful save has been reloaded", async () => {
    let stored = persisted();
    const submitted: LooseObject[] = [];
    serveSettings(() => stored);
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        const values = Object.fromEntries((await request.formData()).entries());
        submitted.push(values);
        stored = {
          ...stored,
          sonarr: {
            ...stored.sonarr,
            ssl: values["settings-sonarr-ssl"] === "true",
          },
        };
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const user = userEvent.setup();
    customRender(
      <Layout name="Test Settings">
        <Check label="Use SSL" settingKey="settings-sonarr-ssl" />
        <TextField label="Address" settingKey="settings-sonarr-ip" />
      </Layout>,
    );

    await waitForHydration();

    const settings = holdSettings(() => stored);
    await user.click(screen.getByLabelText("Use SSL"));
    await user.click(
      await screen.findByRole("button", { name: "Save 1 pending change" }),
    );

    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]["settings-sonarr-ssl"]).toBe("true");

    // The reload a save triggers is the one refresh that is meant to clear the
    // form: what was staged is what the backend now holds.
    await settings.reached;
    settings.release();

    await waitFor(() => {
      expect(
        screen.queryByRole("button", { name: /save/i }),
      ).not.toBeInTheDocument();
    });
    expect(screen.getByLabelText("Use SSL")).toBeChecked();
  });
});
