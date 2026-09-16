import userEvent, { UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import OnboardingWizardView from "./OnboardingWizard";

// Navigation + the settings mutation are the only external effects we assert.
const navigate = vi.fn();
const mutate = vi.fn();

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the settings mutation we assert on.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

describe("OnboardingWizardView", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("renders the Welcome step at step 0", () => {
    customRender(<OnboardingWizardView />);

    expect(
      screen.getByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /get started/i }),
    ).toBeInTheDocument();
  });

  it("advances past Welcome when Get started is clicked", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /get started/i }));

    await waitFor(() => {
      expect(localStorage.getItem("bazarr.onboarding.step")).toBe("1");
    });
    // Welcome hands straight over to the one question the rest follows from.
    expect(
      await screen.findByRole("heading", { name: /what do you want bazarr/i }),
    ).toBeInTheDocument();
  });

  async function answerIntent(user: UserEvent, answer: RegExp) {
    await user.click(screen.getByRole("button", { name: /get started/i }));
    await user.click(await screen.findByRole("radio", { name: answer }));
    await user.click(screen.getByRole("button", { name: /^continue$/i }));
  }

  it("the library path walks the arr steps", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);

    expect(
      await screen.findByRole("heading", { name: /^sonarr$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/step 3 of 12/i)).toBeInTheDocument();
  });

  it("the discover path asks for no arr instance at all", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /find subtitles for anything/i);

    // Seerr, not Sonarr: it is what makes the request button on a title work,
    // and this is the reader most likely to use it.
    expect(
      await screen.findByRole("heading", { name: /^seerr$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/step 3 of 8/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: /^sonarr$/i }),
    ).not.toBeInTheDocument();
  });

  it("changing the answer re-filters the rail and keeps the answer", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    // Back onto the question: the previous answer is still the selected one,
    // so changing it is a choice rather than a fresh guess.
    await user.click(screen.getByRole("button", { name: /back/i }));
    const library = await screen.findByRole("radio", {
      name: /sonarr, radarr or sportarr/i,
    });
    expect(library).toBeChecked();

    await user.click(
      screen.getByRole("radio", { name: /find subtitles for anything/i }),
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    expect(
      await screen.findByRole("heading", { name: /^seerr$/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/step 3 of 8/i)).toBeInTheDocument();
  });

  it("the shell renders the skip control for an optional step", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await answerIntent(user, /sonarr, radarr or sportarr/i);
    await screen.findByRole("heading", { name: /^sonarr$/i });

    await user.click(screen.getByRole("button", { name: /skip this step/i }));

    expect(
      await screen.findByRole("heading", { name: /^radarr$/i }),
    ).toBeInTheDocument();
  });

  it("a step that cannot be skipped says why instead", async () => {
    const user = userEvent.setup();
    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /get started/i }));
    await screen.findByRole("heading", { name: /what do you want bazarr/i });

    expect(
      screen.queryByRole("button", { name: /skip this step/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/nothing here is locked in/i)).toBeInTheDocument();
  });

  it("Finish does not re-filter the rail out from under the reader", async () => {
    // Clearing the intent through React state would re-filter the rail while
    // the step index still belongs to the path being left, so the reader would
    // watch the wizard jump back to a step they already finished in the moment
    // between pressing Finish and the app taking over.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem("bazarr.onboarding.step", "7");
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<OnboardingWizardView />);

    await screen.findByRole("heading", { name: /you are all set/i });
    await user.click(
      screen.getByRole("button", { name: /finish and open discover/i }),
    );
    onSuccess?.();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/discover"));
    expect(
      screen.getByRole("heading", { name: /you are all set/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/step 8 of 8/i)).toBeInTheDocument();
  });

  it("Skip setup saves setup_complete and navigates home", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<OnboardingWizardView />);

    await user.click(screen.getByRole("button", { name: /skip setup/i }));

    expect(mutate).toHaveBeenCalledWith(
      { "settings-general-setup_complete": true },
      expect.anything(),
    );

    // Drive the mutation's onSuccess to verify the navigation side-effect.
    onSuccess?.();

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith("/");
    });
  });
});
