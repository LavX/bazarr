/* eslint-disable camelcase */

import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import App from "@/App";
import { latestWhatsNewVersion } from "@/data/whatsNew";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { WHATS_NEW_SEEN_KEY } from "@/utilities/whatsNew";
import AutopulseSelector from "./AutopulseSelector";

// The panel reads a signed-in Plex account, and then Bazarr no longer holds its
// token, say because it was signed out somewhere else. Generating the
// configuration then needs a Plex sign-in. The app shell sends the reader to
// the login page on any 401, so these run the panel inside that shell and
// watch the Bazarr session, not only the panel.
describe("Plex AutopulseSelector without a Plex sign-in", () => {
  const PAGE = "/settings/connections";
  const GENERATE = "Generate Configuration";
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
            revision: "plex-autopulse",
            locale: "en-US",
            message: "Available",
            checked_at: null,
            fetched_at: null,
          },
        }),
      ),
      // Signed in when the panel first reads the account, and signed out on
      // any later read, as the server answers once the token is gone.
      http.get("/api/plex/oauth/validate", () => {
        validations += 1;
        return HttpResponse.json({
          data:
            validations === 1
              ? { valid: true, auth_method: "oauth", username: "someone" }
              : { valid: false, auth_method: "oauth" },
        });
      }),
    );
  });

  afterEach(() => {
    window.removeEventListener("app-auth-changed", onAuth);
    localStorage.removeItem(WHATS_NEW_SEEN_KEY);
    // A 401 marks the shared client signed out; leave it signed in.
    setAuthenticated(true);
  });

  function renderPage() {
    const router = createMemoryRouter(
      [
        {
          path: "/",
          element: <App />,
          children: [
            {
              path: "*",
              element: <AutopulseSelector label="Autopulse Configuration" />,
            },
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
    server.use(
      http.get("/api/plex/autopulse/config", () =>
        HttpResponse.json(
          {
            error: "Sign in to Plex to generate an Autopulse configuration.",
            error_code: "sign_in_required",
          },
          { status: 409 },
        ),
      ),
    );
    const { router, user } = renderPage();

    await user.click(await screen.findByRole("button", { name: GENERATE }));

    expect(
      await screen.findByText(
        "Sign in to Plex to generate an Autopulse configuration.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/Failed to generate Autopulse configuration/),
    ).not.toBeInTheDocument();
    // The account above was read before the token went, so it is read again,
    // and the panel then asks for a Plex sign-in in place of the button.
    await waitFor(() => expect(validations).toBeGreaterThan(1));
    expect(
      await screen.findByText(
        /Enable Plex OAuth above to generate an Autopulse configuration/,
      ),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: GENERATE }),
    ).not.toBeInTheDocument();
    expect(sessionChanges).not.toContain(false);
    expect(router.state.location.pathname).toBe(PAGE);
  });

  it("still leaves the app when the Bazarr session itself has ended", async () => {
    // What the call used to answer for a missing Plex token. Any 401 means
    // Bazarr's own session is gone, and the shell rightly goes to the login
    // page, so this is also what the test above would see if the Plex answer
    // were ever a 401 again.
    server.use(
      http.get("/api/plex/autopulse/config", () =>
        HttpResponse.json({ error: "Unauthorized" }, { status: 401 }),
      ),
    );
    const { router, user } = renderPage();

    const generate = await screen.findByRole("button", { name: GENERATE });
    await user.click(generate);

    await waitFor(() => expect(sessionChanges).toContain(false));
    await waitFor(() => expect(router.state.location.pathname).toBe("/login"));
    // Done asking, and with nothing to say about Plex or Autopulse: the login
    // page is the answer.
    await waitFor(() => expect(generate).toBeEnabled());
    expect(
      screen.queryByText(/Plex OAuth authentication required/),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/Failed to generate Autopulse configuration/),
    ).not.toBeInTheDocument();
  });
});
