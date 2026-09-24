import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
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

  it("forgets the step, intent and drafts a previous run left behind", async () => {
    // A browser that still held a later step reopened the wizard there, so
    // Run first-time setup did not start at Welcome.
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.step", "sonarr");
    localStorage.setItem("bazarr.onboarding.intent", "library");
    localStorage.setItem("bazarr.onboarding.media-servers", "[]");
    let storedAtNavigate: (string | null)[] = [];
    navigate.mockImplementation(() => {
      storedAtNavigate = ["step", "intent", "media-servers"].map((name) =>
        localStorage.getItem(`bazarr.onboarding.${name}`),
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
    expect(storedAtNavigate).toEqual([null, null, null]);
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
