import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import OnboardingWizardView from "./OnboardingWizard";

// The real router here, deliberately: what is being tested is that the wizard
// has history of its own, which a mocked useNavigate cannot show.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

function openWizardAt(path: string) {
  const router = createMemoryRouter(
    [
      { path: "/setup", element: <OnboardingWizardView /> },
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/", element: <div>home</div> },
    ],
    { initialEntries: [path] },
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

describe("the wizard's own URL", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(),
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("puts the step in the URL", async () => {
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
  });

  it("opens the step the URL names, over the persisted cursor", async () => {
    localStorage.setItem("bazarr.onboarding.step", "welcome");

    openWizardAt("/setup/languages");

    expect(
      await screen.findByRole("heading", { name: /subtitle languages/i }),
    ).toBeInTheDocument();
  });

  it("browser Back moves one step instead of leaving the application", async () => {
    // Every step rendered at /setup and the Redirector arrived with replace,
    // so there was no history entry behind the wizard at all: pressing Back
    // left the application.
    const user = userEvent.setup();
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/intent"),
    );

    await router.navigate(-1);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });

  it("the wizard's own Back does not turn browser Back into forward", async () => {
    // Pushing the earlier step on top of the later one made the next browser
    // Back walk forward through the wizard.
    const user = userEvent.setup();
    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    await user.click(screen.getByRole("button", { name: /^back$/i }));
    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );

    await router.navigate(-1);

    // Out of the wizard the way it was entered, not forward into it again.
    await waitFor(() =>
      expect(router.state.location.pathname).not.toBe("/setup/intent"),
    );
  });

  it("falls back to the persisted cursor when the URL names no step", async () => {
    localStorage.setItem("bazarr.onboarding.step", "languages");

    const router = openWizardAt("/setup");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/languages"),
    );
  });

  it("a step the run does not walk lands on a real step", async () => {
    const router = openWizardAt("/setup/not-a-step");

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/welcome"),
    );
    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
  });
});
