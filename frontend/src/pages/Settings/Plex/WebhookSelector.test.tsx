/* eslint-disable camelcase */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import App from "@/App";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { customRender, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";
import WebhookSelector from "./WebhookSelector";

// The listing answers an account without Plex Pass with an empty list and the
// plan it has. It used to answer 502, which left the page showing "Failed to
// load webhooks" instead of saying why there are none.
function plexAccount(plexPassSubscription: Plex.PlexPassSubscription) {
  server.use(
    http.get("/api/plex/oauth/validate", () =>
      HttpResponse.json({
        data: { valid: true, auth_method: "oauth", username: "someone" },
      }),
    ),
    http.get("/api/plex/webhook/list", () =>
      HttpResponse.json({
        data: { webhooks: [], count: 0, plexPassSubscription },
      }),
    ),
  );
  customRender(<WebhookSelector label="Webhooks" />);
}

describe("Plex WebhookSelector", () => {
  it("says webhooks need Plex Pass and offers no Add without it", async () => {
    plexAccount({ active: false, has_webhooks_feature: false, plan: null });

    expect(
      await screen.findByText(/Webhooks require a Plex Pass subscription/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeDisabled();
    expect(
      screen.queryByText(/Failed to load webhooks/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/No webhooks found on your Plex server/),
    ).not.toBeInTheDocument();
  });

  it("lets a Plex Pass account add its first webhook", async () => {
    plexAccount({ active: true, has_webhooks_feature: true, plan: "lifetime" });

    expect(
      await screen.findByText(/No webhooks found on your Plex server/),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add" })).toBeEnabled();
    expect(
      screen.queryByText(/Webhooks require a Plex Pass subscription/),
    ).not.toBeInTheDocument();
  });
});

// Bazarr holds no Plex token, while the panel still reads a signed-in account
// from before. The webhook calls answer that with a 409 and its own code. The
// app shell sends the reader to the login page on any 401, so these run the
// panel inside that shell and watch the Bazarr session, not only the panel.
describe("Plex WebhookSelector without a Plex sign-in", () => {
  const PAGE = "/settings/connections";
  const signInRequired = () =>
    HttpResponse.json(
      {
        error: "Sign in to Plex to manage webhooks.",
        error_code: "sign_in_required",
      },
      { status: 409 },
    );
  let sessionChanges: boolean[] = [];
  let validations = 0;
  const onAuth = (event: WindowEventMap["app-auth-changed"]) =>
    sessionChanges.push(event.detail.authenticated);

  beforeEach(() => {
    sessionChanges = [];
    validations = 0;
    localStorage.setItem(WHATS_NEW_SEEN_KEY, latestWhatsNewVersion);
    setAuthenticated(true);
    window.addEventListener("app-auth-changed", onAuth);
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "dark", setup_complete: true },
          auth: { type: "form" },
        }),
      ),
      http.get("/api/badges", () => HttpResponse.json({})),
      http.get("/api/system/status", () =>
        HttpResponse.json({ data: { bazarr_version: "test" } }),
      ),
      http.get("/api/system/jobs", () => HttpResponse.json({ data: [] })),
      // The shell's title search asks for this on every page.
      http.get("/api/discover/metadata/status", () =>
        HttpResponse.json({
          data: {
            source: "tmdb",
            status: "available",
            configured: true,
            revision: "plex-webhooks",
            locale: "en-US",
            message: "Available",
            checked_at: null,
            fetched_at: null,
          },
        }),
      ),
      http.get("/api/plex/oauth/validate", () => {
        validations += 1;
        return HttpResponse.json({
          data: { valid: true, auth_method: "oauth", username: "someone" },
        });
      }),
    );
  });

  afterEach(() => {
    window.removeEventListener("app-auth-changed", onAuth);
    localStorage.removeItem(WHATS_NEW_SEEN_KEY);
  });

  function renderPage() {
    const router = createMemoryRouter(
      [
        {
          path: "/",
          element: <App />,
          children: [
            { path: "*", element: <WebhookSelector label="Webhooks" /> },
          ],
        },
      ],
      { initialEntries: [PAGE] },
    );
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    return { router, user: userEvent.setup() };
  }

  it("asks to sign in to Plex and keeps the Bazarr session", async () => {
    server.use(http.get("/api/plex/webhook/list", signInRequired));
    const { router } = renderPage();

    await waitFor(() =>
      expect(
        queryClient.getQueryState([QueryKeys.Plex, "webhooks"])?.status,
      ).toBe("error"),
    );
    expect(sessionChanges).not.toContain(false);
    expect(router.state.location.pathname).toBe(PAGE);

    expect(
      await screen.findByText(/Sign in to Plex above to manage webhooks/),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Failed to load webhooks/),
    ).not.toBeInTheDocument();
    // The panel above read a sign-in that is gone, so it reads it again.
    await waitFor(() => expect(validations).toBeGreaterThan(1));
    expect(sessionChanges).not.toContain(false);
    expect(router.state.location.pathname).toBe(PAGE);
  });

  it("says Add needs a Plex sign-in and keeps the Bazarr session", async () => {
    let listed = 0;
    server.use(
      http.get("/api/plex/webhook/list", () => {
        listed += 1;
        return listed === 1
          ? HttpResponse.json({
              data: {
                webhooks: [],
                count: 0,
                plexPassSubscription: {
                  active: true,
                  has_webhooks_feature: true,
                  plan: "lifetime",
                },
              },
            })
          : signInRequired();
      }),
      http.post("/api/plex/webhook/create", signInRequired),
    );
    const { router, user } = renderPage();

    await user.click(await screen.findByRole("button", { name: "Add" }));

    expect(
      await screen.findByText("Sign in to Plex to manage webhooks."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Failed to create webhook"),
    ).not.toBeInTheDocument();
    expect(
      await screen.findByText(/Sign in to Plex above to manage webhooks/),
    ).toBeInTheDocument();
    expect(sessionChanges).not.toContain(false);
    expect(router.state.location.pathname).toBe(PAGE);
  });
});
