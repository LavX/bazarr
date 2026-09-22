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
