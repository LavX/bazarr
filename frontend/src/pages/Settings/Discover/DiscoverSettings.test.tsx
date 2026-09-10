import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it, vi } from "vitest";
import Layout from "@/pages/Settings/components/Layout";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const log = vi.hoisted(() => vi.fn());
vi.mock("@/utilities/console", async (original) => ({
  ...(await original<object>()),
  LOG: log,
}));

function CredentialDraft() {
  const { setValue } = useFormActions();
  return (
    <button
      type="button"
      onClick={() =>
        setValue(
          "synthetic-private-draft",
          "settings-discover-tmdb_access_token",
        )
      }
    >
      Stage token
    </button>
  );
}

it("redacts the write-only credential at form and submit log boundaries while retaining the submitted value", async () => {
  const submitted: FormData[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  customRender(
    <Layout name="Discover">
      <CredentialDraft />
    </Layout>,
  );
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: "Stage token" }));
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );
  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0].get("settings-discover-tmdb_access_token")).toBe(
    "synthetic-private-draft",
  );
  expect(JSON.stringify(log.mock.calls)).not.toContain(
    "synthetic-private-draft",
  );
});

/* eslint-disable camelcase -- API fixture fields. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { modals } from "@mantine/modals";
import { notifications } from "@mantine/notifications";
import { beforeEach } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { DiscoverSetupReturn } from "@/contexts/Discover";
import Discover from "@/pages/Discover";
import { AllProviders } from "@/providers";
import { act, rawRender } from "@/tests";
import DiscoverSettings from ".";

const savedSettings = {
  general: { theme: "auto" },
  discover: {
    tmdb_configured: true,
    tmdb_token_stored: true,
    metadata_revision: "metadata-one",
    locale: "en-US",
  },
};
const connection = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "metadata-one",
  locale: "en-US",
  message: "TMDB is available.",
  checked_at: "2026-09-08T10:00:00Z",
  fetched_at: null,
};

beforeEach(() => {
  notifications.clean();
  modals.closeAll();
  log.mockClear();
  localStorage.clear();
  server.use(
    http.get("/api/system/settings", () => HttpResponse.json(savedSettings)),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: connection }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...connection, items: [] } }),
    ),
  );
});

async function renderSettings(fromDiscover = false) {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      {
        path: "/settings/discover",
        element: (
          <DiscoverSetupReturn>
            <DiscoverSettings />
          </DiscoverSetupReturn>
        ),
      },
    ],
    {
      initialEntries: [
        fromDiscover ? "/discover?view=movies#browsing" : "/settings/discover",
      ],
    },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  if (!fromDiscover)
    await waitFor(() =>
      expect(
        screen.getByLabelText("Your own TMDB API key, optional"),
      ).toBeEnabled(),
    );
  return { router, user: userEvent.setup() };
}

it("starts blank, cancels type-then-clear, and requires explicit removal", async () => {
  const { user } = await renderSettings();
  const token = await screen.findByLabelText("Your own TMDB API key, optional");
  expect(token).toHaveValue("");
  expect(
    screen.queryByRole("button", { name: /Save 1 pending/ }),
  ).not.toBeInTheDocument();
  await user.type(token, "draft");
  await user.clear(token);
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /Save 1 pending/ }),
    ).not.toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: "Remove saved key" }));
  expect(await screen.findByText(/Removal pending save/)).toBeInTheDocument();
  expect(
    screen.getByRole("button", { name: /Save 1 pending/ }),
  ).toBeInTheDocument();
});

it.each(["button", "enter", "shortcut"])(
  "saves only the replacement through %s and resets the private draft",
  async (method) => {
    const submitted: FormData[] = [];
    server.use(
      http.post("/api/system/settings", async ({ request }) => {
        submitted.push(await request.formData());
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const { user } = await renderSettings();
    const token = await screen.findByLabelText(
      "Your own TMDB API key, optional",
    );
    await user.type(token, "replacement-private");
    if (method === "button")
      await user.click(
        await screen.findByRole("button", { name: /Save 1 pending/ }),
      );
    if (method === "enter") await user.keyboard("{Enter}");
    if (method === "shortcut") await user.keyboard("{Control>}s{/Control}");
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect([...submitted[0].entries()]).toEqual([
      ["settings-discover-tmdb_access_token", "replacement-private"],
    ]);
    await waitFor(() => expect(token).toHaveValue(""));
    expect(JSON.stringify(log.mock.calls)).not.toContain("replacement-private");
  },
);

it("omits untouched token from an ordinary locale save and submits empty only for explicit removal", async () => {
  const submitted: FormData[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  const { user } = await renderSettings();
  await user.selectOptions(
    await screen.findByLabelText("Metadata language"),
    "hu-HU",
  );
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );
  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0].has("settings-discover-tmdb_access_token")).toBe(false);
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: /Save 1 pending/ }),
    ).not.toBeInTheDocument(),
  );
  await user.click(screen.getByRole("button", { name: "Remove saved key" }));
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );
  await waitFor(() => expect(submitted).toHaveLength(2));
  expect(submitted[1].get("settings-discover-tmdb_access_token")).toBe("");
});

it("tests saved and unsaved credentials without saving and retires late draft checks", async () => {
  const bodies: unknown[] = [];
  let release: (() => void) | undefined;
  server.use(
    http.post("/api/discover/metadata/test", async ({ request }) => {
      const body = await request.json();
      bodies.push(body);
      if (bodies.length === 2)
        await new Promise<void>((resolve) => {
          release = resolve;
        });
      return HttpResponse.json({ data: connection });
    }),
  );
  const { user } = await renderSettings();
  await user.click(
    await screen.findByRole("button", { name: "Check saved connection" }),
  );
  await screen.findByText("TMDB is available.");
  expect(bodies).toEqual([{}]);
  await user.type(
    screen.getByLabelText("Your own TMDB API key, optional"),
    "old-draft",
  );
  await user.click(
    screen.getByRole("button", { name: "Check draft connection" }),
  );
  await waitFor(() => expect(release).toBeDefined());
  await user.type(
    screen.getByLabelText("Your own TMDB API key, optional"),
    "-changed",
  );
  release?.();
  expect(screen.queryByText("TMDB is available.")).not.toBeInTheDocument();
  expect(bodies[1]).toEqual({ token: "old-draft" });
  expect(screen.getByLabelText("Your own TMDB API key, optional")).toHaveValue(
    "old-draft-changed",
  );
});

it("keeps failed-save draft through settings events", async () => {
  server.use(
    http.post(
      "/api/system/settings",
      () => new HttpResponse(null, { status: 503 }),
    ),
  );
  const { user, router } = await renderSettings();
  await user.type(
    await screen.findByLabelText("Your own TMDB API key, optional"),
    "retained-private",
  );
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );
  await screen.findByText("Save failed");
  await queryClient.refetchQueries({
    queryKey: [QueryKeys.System, QueryKeys.Settings],
  });
  expect(screen.getByLabelText("Your own TMDB API key, optional")).toHaveValue(
    "retained-private",
  );
  expect(router.state.location.pathname).toBe("/settings/discover");
});

it("preserves the real return route, query and focus through Keep editing, a failed Save and leave and a successful retry", async () => {
  let count = 0;
  server.use(
    http.post("/api/system/settings", () => {
      count++;
      return new HttpResponse(null, { status: count === 1 ? 503 : 204 });
    }),
  );
  const { user, router } = await renderSettings(true);
  await user.type(screen.getByLabelText("Search movie titles"), "Shogun");
  await user.click(screen.getByRole("link", { name: "Discover settings" }));
  await user.type(
    await screen.findByLabelText("Your own TMDB API key, optional"),
    "pending-private",
  );
  await user.click(screen.getByRole("link", { name: "Return to Discover" }));
  await user.click(
    await screen.findByRole("button", {
      name: "Keep editing",
    }),
  );
  expect(router.state.location.pathname).toBe("/settings/discover");
  expect(screen.getByLabelText("Your own TMDB API key, optional")).toHaveValue(
    "pending-private",
  );
  await user.click(screen.getByRole("link", { name: "Return to Discover" }));
  await user.click(
    await screen.findByRole("button", {
      name: "Save and leave",
    }),
  );
  await screen.findByText("Save failed");
  expect(router.state.location.pathname).toBe("/settings/discover");
  expect(screen.getByLabelText("Your own TMDB API key, optional")).toHaveValue(
    "pending-private",
  );
  await user.click(
    screen.getByRole("button", {
      name: "Save and leave",
    }),
  );
  await waitFor(() => expect(router.state.location.pathname).toBe("/discover"));
  expect(router.state.location.search + router.state.location.hash).toBe(
    "?view=movies#browsing",
  );
  expect(await screen.findByLabelText("Search movie titles")).toHaveValue(
    "Shogun",
  );
  await waitFor(() =>
    expect(
      screen.getByRole("link", { name: "Discover settings" }),
    ).toHaveFocus(),
  );
});

it("discards a replacement through the real modal without saving and returns with browsing intact", async () => {
  const saves: unknown[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      saves.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  const { user, router } = await renderSettings(true);
  await user.type(screen.getByLabelText("Search movie titles"), "Shogun");
  await user.click(screen.getByRole("link", { name: "Discover settings" }));
  await user.type(
    await screen.findByLabelText("Your own TMDB API key, optional"),
    "abandoned-private",
  );
  await user.click(screen.getByRole("link", { name: "Return to Discover" }));
  await user.click(
    await screen.findByRole("button", {
      name: "Discard changes",
    }),
  );
  await waitFor(() => expect(router.state.location.pathname).toBe("/discover"));
  expect(await screen.findByLabelText("Search movie titles")).toHaveValue(
    "Shogun",
  );
  expect(saves).toEqual([]);
  await user.click(screen.getByRole("link", { name: "Discover settings" }));
  expect(
    await screen.findByLabelText("Your own TMDB API key, optional"),
  ).toHaveValue("");
});

it("retains a failed metadata-language draft through a settings refetch", async () => {
  server.use(
    http.post(
      "/api/system/settings",
      () => new HttpResponse(null, { status: 503 }),
    ),
  );
  const { user } = await renderSettings();
  await user.selectOptions(
    await screen.findByLabelText("Metadata language"),
    "hu-HU",
  );
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending/ }),
  );
  await screen.findByText("Save failed");
  let release: (() => void) | undefined;
  server.use(
    http.get("/api/system/settings", async () => {
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return HttpResponse.json(savedSettings);
    }),
  );
  const refetch = queryClient.refetchQueries({
    queryKey: [QueryKeys.System, QueryKeys.Settings],
  });
  await waitFor(() => expect(release).toBeDefined());
  await act(async () => {
    release?.();
    await refetch;
  });
  await user.click(screen.getByRole("heading", { name: "Discover" }));
  expect(screen.getByLabelText("Metadata language")).toHaveValue("hu-HU");
  expect(
    screen.getByRole("button", { name: /Save 1 pending/ }),
  ).toBeInTheDocument();
});

it.each(["retry", "leave", "ordinary save", "failed retry"])(
  "reports saved settings truthfully after refresh failure, then supports %s",
  async (action) => {
    const submitted: FormData[] = [];
    let persisted = false;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json(
          persisted
            ? {
                ...savedSettings,
                discover: {
                  ...savedSettings.discover,
                  metadata_revision: "saved-new",
                  locale: "hu-HU",
                },
              }
            : savedSettings,
        ),
      ),
      http.get("/api/discover/metadata/status", () =>
        HttpResponse.json({
          data: {
            ...connection,
            revision: persisted ? "saved-new" : "metadata-one",
            locale: persisted ? "hu-HU" : "en-US",
          },
        }),
      ),
      http.post("/api/system/settings", async ({ request }) => {
        submitted.push(await request.formData());
        persisted = true;
        if (action === "failed retry" && submitted.length === 2) {
          return new HttpResponse(null, { status: 503 });
        }
        return submitted.length === 1
          ? HttpResponse.json(
              {
                code: "discover_settings_refresh_failed",
                message:
                  "Discover settings were saved, but application refresh failed. Reload settings before retrying.",
              },
              { status: 503 },
            )
          : new HttpResponse(null, { status: 204 });
      }),
    );
    const { user, router } = await renderSettings(true);
    await user.click(screen.getByRole("link", { name: "Discover settings" }));
    await user.type(
      await screen.findByLabelText("Your own TMDB API key, optional"),
      "saved-private-replacement",
    );
    await user.selectOptions(
      screen.getByLabelText("Metadata language"),
      "hu-HU",
    );
    if (action === "ordinary save") {
      await user.click(screen.getByRole("button", { name: /Save 2 pending/ }));
    } else {
      await user.click(
        screen.getByRole("link", { name: "Return to Discover" }),
      );
      await user.click(
        await screen.findByRole("button", {
          name: "Save and leave",
        }),
      );
    }
    await screen.findByText("Settings saved; application refresh failed");
    expect(router.state.location.pathname).toBe("/settings/discover");
    expect(
      screen.getByLabelText("Your own TMDB API key, optional"),
    ).toHaveValue("");
    expect(
      screen.queryByRole("button", { name: /Save \d+ pending/ }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Unsaved Changes")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        queryClient.getQueryData([QueryKeys.System, QueryKeys.Settings]),
      ).toMatchObject({
        discover: { metadata_revision: "saved-new", locale: "hu-HU" },
      }),
    );
    expect(
      queryClient.getQueryCache().findAll({
        queryKey: [QueryKeys.Discover, "metadata", "metadata-one"],
      }),
    ).toHaveLength(0);
    if (action === "ordinary save") {
      await user.click(
        screen.getByRole("link", { name: "Return to Discover" }),
      );
    }
    if (action === "retry" || action === "failed retry") {
      await user.click(
        await screen.findByRole("button", {
          name: "Retry refresh and leave",
        }),
      );
      if (action === "failed retry") {
        await waitFor(() => expect(submitted).toHaveLength(2));
        expect(router.state.location.pathname).toBe("/settings/discover");
        expect(
          screen.getByLabelText("Your own TMDB API key, optional"),
        ).toHaveValue("");
        expect(screen.queryByText("Save failed")).not.toBeInTheDocument();
        await user.click(
          screen.getByRole("button", {
            name: "Leave with saved settings",
          }),
        );
      }
    } else {
      await user.click(
        await screen.findByRole("button", {
          name: "Leave with saved settings",
        }),
      );
    }
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/discover"),
    );
    expect(router.state.location.search + router.state.location.hash).toBe(
      "?view=movies#browsing",
    );
    expect(submitted).toHaveLength(
      action === "retry" || action === "failed retry" ? 2 : 1,
    );
    if (submitted.length === 2) expect([...submitted[1].entries()]).toEqual([]);
    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    expect(JSON.stringify(log.mock.calls)).not.toContain(
      "saved-private-replacement",
    );
  },
);

// Metadata is available on the built-in key whether or not the reader saved
// anything, so availability stopped being evidence that there is a saved key.
// A reader who never saved one must not be offered its removal.
it("offers removal only to a reader who actually saved a key", async () => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        ...savedSettings,
        discover: { ...savedSettings.discover, tmdb_token_stored: false },
      }),
    ),
  );
  await renderSettings();
  expect(
    await screen.findByRole("button", { name: "Check built-in connection" }),
  ).toBeInTheDocument();
  expect(screen.getByText("TMDB metadata is available")).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Remove saved key" }),
  ).not.toBeInTheDocument();
});
