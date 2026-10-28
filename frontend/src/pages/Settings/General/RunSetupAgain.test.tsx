import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { PendingChangesProvider } from "@/contexts";
import { readOnboardingValue } from "@/pages/Setup/onboardingStorage";
import OnboardingWizardView from "@/pages/Setup/OnboardingWizard";
import { customRender, screen, waitFor } from "@/tests";
import RunSetupAgain from "./RunSetupAgain";

const navigate = vi.fn();
const mutate = vi.fn();

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

describe("RunSetupAgain", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    localStorage.clear();
  });

  it("clears the flag and reopens the wizard", async () => {
    // Nothing in the application linked to /setup, so leaving setup was
    // permanent unless the reader knew to type a URL. This is the way back the
    // wizard's own confirmation promises.
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<RunSetupAgain />);

    await user.click(
      screen.getByRole("button", { name: /run first-time setup/i }),
    );

    expect(mutate).toHaveBeenCalledWith(
      { "settings-general-setup_complete": false },
      expect.anything(),
    );
    expect(navigate).not.toHaveBeenCalled();

    onSuccess?.();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/setup"));
  });

  it("writes nothing while the settings page holds unsaved changes", async () => {
    // The page answers the navigation with its unsaved-changes prompt, and
    // Keep editing used to leave the reader on Settings with the flag already
    // cleared, so the next visit to the home page opened the wizard.
    const user = userEvent.setup();

    customRender(
      <PendingChangesProvider value>
        <RunSetupAgain />
      </PendingChangesProvider>,
    );

    const button = screen.getByRole("button", {
      name: /run first-time setup/i,
    });
    // Unavailable but still in the tab order, so the reason is reachable.
    expect(button).toHaveAttribute("aria-disabled", "true");
    expect(button).not.toBeDisabled();
    expect(
      screen.getByText(/save or discard your unsaved changes/i),
    ).toBeVisible();
    expect(button).toHaveAccessibleDescription(/save or discard/i);
    await user.tab();
    expect(button).toHaveFocus();
    await user.click(button);

    expect(mutate).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });

  it("is available again once the changes are saved or discarded", () => {
    const view = customRender(
      <PendingChangesProvider value>
        <RunSetupAgain />
      </PendingChangesProvider>,
    );
    view.rerender(
      <PendingChangesProvider value={false}>
        <RunSetupAgain />
      </PendingChangesProvider>,
    );

    const button = screen.getByRole("button", {
      name: /run first-time setup/i,
    });
    expect(button).toBeEnabled();
    expect(button).not.toHaveAttribute("aria-disabled");
    expect(screen.queryByText(/save or discard/i)).toBeNull();
  });

  it("forgets the step, intent and drafts a previous run left behind", async () => {
    // A browser that still held a later step reopened the wizard there, so
    // Run first-time setup did not start at Welcome.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.step", "sonarr");
    localStorage.setItem("bazarr.onboarding.intent", "library");
    localStorage.setItem("bazarr.onboarding.media-servers", "[]");
    localStorage.setItem("bazarr.onboarding.skipped", '["sonarr"]');
    let storedAtNavigate: (string | null)[] = [];
    navigate.mockImplementation(() => {
      storedAtNavigate = ["step", "intent", "media-servers", "skipped"].map(
        (name) => localStorage.getItem(`bazarr.onboarding.${name}`),
      );
    });
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(<RunSetupAgain />);

    await user.click(
      screen.getByRole("button", { name: /run first-time setup/i }),
    );

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/setup"));
    expect(storedAtNavigate).toEqual([null, null, null, null]);
  });

  it("reopens at Welcome under a base URL too", async () => {
    // An install served under a subpath adopts what an earlier version left
    // under the plain keys, and it did so again after every clear, because a
    // cleared key looked exactly like one that was never written. The wizard
    // came back on the old step with the old answer.
    const user = userEvent.setup();
    const names = ["step", "intent", "media-servers"];
    const earlier = {
      step: "sonarr",
      intent: "library",
      "media-servers": "[]",
    };
    for (const [name, value] of Object.entries(earlier)) {
      localStorage.setItem(`bazarr.onboarding.${name}`, value);
    }
    vi.stubGlobal("Bazarr", { baseUrl: "/bazarr" });
    // Read once, as an earlier visit would have, so this install has adopted
    // its own copies.
    expect(names.map(readOnboardingValue)).toEqual(Object.values(earlier));

    let storedAtNavigate: (string | null)[] = [];
    navigate.mockImplementation(() => {
      storedAtNavigate = names.map(readOnboardingValue);
    });
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    const view = customRender(<RunSetupAgain />);
    await user.click(
      screen.getByRole("button", { name: /run first-time setup/i }),
    );

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/setup"));
    expect(storedAtNavigate).toEqual([null, null, null]);

    view.unmount();
    customRender(<OnboardingWizardView />);

    expect(
      await screen.findByRole("heading", { name: /welcome to bazarr/i }),
    ).toBeInTheDocument();
    // The install served from the root still has its own.
    expect(localStorage.getItem("bazarr.onboarding.step")).toBe("sonarr");
  });

  it("says so when the flag cannot be cleared", async () => {
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onError?: (reason: unknown) => void }) => {
        opts?.onError?.(new Error("no"));
      },
    );

    customRender(<RunSetupAgain />);

    await user.click(
      screen.getByRole("button", { name: /run first-time setup/i }),
    );

    // The title and the message both say it; one is enough to prove the
    // failure reached the page.
    expect(
      (await screen.findAllByText(/could not reopen setup/i)).length,
    ).toBeGreaterThan(0);
    expect(navigate).not.toHaveBeenCalled();
  });
});
