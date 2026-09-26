/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import SettingsProvidersView from ".";

beforeEach(() => {
  queryClient.clear();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { theme: "auto", enabled_providers: [] } }),
    ),
    http.get("/api/provider-hub/providers", () =>
      HttpResponse.json({ data: [] }),
    ),
    http.get("/api/provider-hub/catalog", () =>
      HttpResponse.json({ data: { sources: [], entries: [] } }),
    ),
    http.get("/api/provider-hub/jobs", () => HttpResponse.json({ data: [] })),
  );
});

// Discover's readiness notice links here with ?tab=marketplace. Reading that
// parameter only at mount meant the link worked from elsewhere and did nothing
// at all when this page was already open, which is exactly the case the notice
// creates once a reader has been here and switched tabs.
it("opens the marketplace tab from the link every time it is followed", async () => {
  const router = createMemoryRouter(
    [{ path: "/subtitle-hub", element: <SettingsProvidersView /> }],
    { initialEntries: ["/subtitle-hub?tab=marketplace"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  const user = userEvent.setup();
  expect(
    await screen.findByRole("tab", { name: /Marketplace/, selected: true }),
  ).toBeInTheDocument();
  await user.click(screen.getByRole("tab", { name: /My Providers/ }));
  expect(
    screen.getByRole("tab", { name: /My Providers/, selected: true }),
  ).toBeInTheDocument();
  await router.navigate("/subtitle-hub?tab=marketplace");
  expect(
    await screen.findByRole("tab", { name: /Marketplace/, selected: true }),
  ).toBeInTheDocument();
});

it("saves the AI subtitle penalty as a bounded integer setting", async () => {
  const submitted: FormData[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "auto",
          enabled_providers: [],
          ai_translated_score_penalty: 0,
        },
      }),
    ),
    http.post("/api/system/settings", async ({ request }) => {
      submitted.push(await request.formData());
      return new HttpResponse(null, { status: 204 });
    }),
  );
  const router = createMemoryRouter(
    [{ path: "/subtitle-hub", element: <SettingsProvidersView /> }],
    { initialEntries: ["/subtitle-hub"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  const user = userEvent.setup();
  const input = await screen.findByLabelText(
    "AI-translated subtitle penalty (%)",
  );
  await user.clear(input);
  await user.type(input, "10");
  await user.click(
    await screen.findByRole("button", { name: /Save 1 pending change/ }),
  );
  await waitFor(() => expect(submitted).toHaveLength(1));
  expect(submitted[0].get("settings-general-ai_translated_score_penalty")).toBe(
    "10",
  );
});
