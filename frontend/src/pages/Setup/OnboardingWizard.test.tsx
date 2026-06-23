import userEvent from "@testing-library/user-event";
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

    // Step persisted past Welcome (Phase 2 has only one step, so the Welcome
    // heading should no longer be rendered once we advance off it).
    await waitFor(() => {
      expect(localStorage.getItem("bazarr.onboarding.step")).toBe("1");
    });
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
