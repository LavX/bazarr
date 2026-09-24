import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { AllProviders } from "@/providers";
import Redirector from "@/Router/Redirector";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import OnboardingWizardView from "./OnboardingWizard";

// The real router, Redirector and query cache: what broke was the cache the
// Redirector read on arrival, which a mocked navigate cannot show.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mutate = vi.fn();

beforeEach(() => {
  localStorage.clear();
  vi.mocked(useSettingsMutation).mockReturnValue({
    mutate,
  } as unknown as ReturnType<typeof useSettingsMutation>);
});

afterEach(() => {
  localStorage.clear();
});

it("Leave setup lands on Discover, not back in the wizard", async () => {
  // Leaving saved setup_complete and navigated to "/" before the settings read
  // came back, so the Redirector read the old false and reopened the wizard.
  let saved = false;
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { setup_complete: saved } }),
    ),
  );
  let onSuccess: (() => unknown) | undefined;
  mutate.mockImplementation(
    (_input: unknown, opts?: { onSuccess?: () => unknown }) => {
      onSuccess = opts?.onSuccess;
    },
  );

  const router = createMemoryRouter(
    [
      { path: "/", element: <Redirector /> },
      { path: "/setup", element: <OnboardingWizardView /> },
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/discover", element: <div>discover page</div> },
    ],
    { initialEntries: ["/"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );

  await waitFor(() =>
    expect(router.state.location.pathname).toBe("/setup/welcome"),
  );

  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /set up later/i }));
  await user.click(
    await screen.findByRole("button", { name: /^leave setup$/i }),
  );
  expect(mutate).toHaveBeenCalledWith(
    { "settings-general-setup_complete": true },
    expect.anything(),
  );

  saved = true;
  await onSuccess?.();

  expect(await screen.findByText("discover page")).toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/discover");
});
