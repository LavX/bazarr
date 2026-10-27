import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";

// A media server's configure step is generated for its draft and goes the
// moment its save lands, but the browser still holds its URL in the history
// under the step that followed. Back has to walk over it rather than stop on it.

let rows: { id: string; kind: string; name: string }[] = [];

function stageBackend() {
  server.use(
    http.get("/api/system/media-server-instances", ({ request }) => {
      const kind = new URL(request.url).searchParams.get("kind");
      return HttpResponse.json({
        data: rows
          .filter((row) => row.kind === kind)
          .map((row) => ({
            ...row,
            enabled: true,
            url: "http://10.0.0.9:8096",
            verify_ssl: true,
            api_key_set: true,
            path_mappings: [],
            refresh_movies: true,
            refresh_episodes: true,
            options: {},
          })),
      });
    }),
    http.post("/api/system/media-server-instances", async ({ request }) => {
      const body = (await request.json()) as { kind: string; name: string };
      const row = { id: `row-${rows.length + 1}`, ...body };
      rows.push(row);
      return HttpResponse.json({
        ...row,
        enabled: true,
        url: "http://10.0.0.9:8096",
        verify_ssl: true,
        api_key_set: true,
        path_mappings: [],
        refresh_movies: true,
        refresh_episodes: true,
        options: {},
      });
    }),
    http.post(
      "/api/system/settings",
      () => new HttpResponse(null, { status: 204 }),
    ),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ data: { general: {}, translator: {} } }),
    ),
  );
}

function openAtPicker() {
  const router = createMemoryRouter(
    [
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/", element: <div>home</div> },
    ],
    { initialEntries: ["/", "/setup/media-servers"], initialIndex: 1 },
  );
  rawRender(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
  return router;
}

/**
 * Opens straight onto a generated step, the way a reload or a followed link
 * does: the one wizard entry in the history has nothing of the wizard's under
 * it.
 */
function openAtGeneratedStep() {
  localStorage.setItem(
    "bazarr.onboarding.media-servers",
    JSON.stringify([
      { draftId: "d1", kind: "jellyfin", name: "Jellyfin", url: "" },
    ]),
  );
  const router = createMemoryRouter(
    [
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/", element: <div>home</div> },
    ],
    { initialEntries: ["/", "/setup/media-server-d1"], initialIndex: 1 },
  );
  rawRender(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
  return router;
}

/** Ticks Jellyfin, saves it from its generated step and lands on Requests. */
async function saveJellyfin(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("heading", { name: /^media servers$/i });
  const box = screen.getByRole("checkbox", { name: /^Jellyfin$/ });
  await waitFor(() => expect(box).toBeEnabled());
  await user.click(box);
  await user.click(screen.getByRole("button", { name: /set up 1 server/i }));
  await screen.findByRole("heading", { name: /^jellyfin$/i });
  await user.type(screen.getByLabelText(/server url/i), "http://10.0.0.9:8096");
  await user.type(screen.getByLabelText(/api key/i), "jelly-key");
  await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
  await screen.findByRole("heading", { name: /^requests$/i });
}

describe("history after a generated step goes", () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem("bazarr.onboarding.intent", "library");
    rows = [];
    stageBackend();
  });

  afterEach(() => localStorage.clear());

  it("the wizard's own Back walks over the removed step in one move", async () => {
    const user = userEvent.setup();
    const router = openAtPicker();
    await saveJellyfin(user);

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/media-servers"),
    );

    // One press out of the wizard, the way it was entered: the removed step's
    // entry is not left underneath to swallow it.
    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("browser Back from the next step skips the removed one", async () => {
    const user = userEvent.setup();
    const router = openAtPicker();
    await saveJellyfin(user);

    await act(async () => {
      await router.navigate(-1);
    });

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/media-servers"),
    );
    expect(
      await screen.findByRole("heading", { name: /^media servers$/i }),
    ).toBeInTheDocument();

    // And the entry before the picker is still the one the reader came from.
    await act(async () => {
      await router.navigate(-1);
    });
    await waitFor(() => expect(router.state.location.pathname).toBe("/"));
  });

  it("browser Back onto a removed step the run was entered at stays in the wizard", async () => {
    // Entered at the generated step itself, so the only wizard entry under
    // the next step is the one for the step that has since gone, and the
    // entry under that belongs to whatever the reader was on before. Going
    // over the dead entry landed there and left setup on one press of Back.
    const user = userEvent.setup();
    const router = openAtGeneratedStep();

    await screen.findByRole("heading", { name: /^jellyfin$/i });
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "jelly-key");
    await user.click(screen.getByRole("button", { name: /connect jellyfin/i }));
    await screen.findByRole("heading", { name: /^requests$/i });
    // The URL follows the cursor a render later, and a Back sent before the
    // wizard has rendered it is a Back from the wrong entry: the wizard is
    // still waiting for its own move to arrive and ignores the one that came
    // instead. Under load that render can trail the router's own state, so the
    // router settling is not enough and the wizard gets a moment as well.
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/seerr"),
    );
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
    });

    await act(async () => {
      await router.navigate(-1);
    });

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/media-servers"),
    );
    expect(
      await screen.findByRole("heading", { name: /^media servers$/i }),
    ).toBeInTheDocument();
  });
});
