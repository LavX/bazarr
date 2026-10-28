import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { useSeerrTestConnectionMutation } from "@/apis/hooks/seerr";
import { StepBusyProvider, useStepBusy } from "@/pages/Setup/useStepBusy";
import { customRender, screen } from "@/tests";
import SeerrStep from "./SeerrStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the hooks the step drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

vi.mock("@/apis/hooks/seerr", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks/seerr")>();
  return {
    ...actual,
    useSeerrTestConnectionMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);
const mockedTestMutation = vi.mocked(useSeerrTestConnectionMutation);

const onNext = vi.fn();
const mutate = vi.fn();
const testMutate = vi.fn();

function ShellBusy() {
  return <span>{useStepBusy() ? "step busy" : "step free"}</span>;
}

function StepWithBusyProbe() {
  return (
    <StepBusyProvider>
      <SeerrStep onNext={onNext} />
      <ShellBusy />
    </StepBusyProvider>
  );
}

function setTestState(state: Record<string, unknown>) {
  mockedTestMutation.mockReturnValue({
    mutate: testMutate,
    isPending: false,
    isError: false,
    data: undefined,
    ...state,
  } as unknown as ReturnType<typeof useSeerrTestConnectionMutation>);
}

describe("SeerrStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setTestState({});
    mockedSettingsMutation.mockReturnValue({
      mutate,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("reports busy while the settings save is pending and clears after it settles", () => {
    mockedSettingsMutation.mockReturnValue({
      mutate,
      isPending: true,
    } as unknown as ReturnType<typeof useSettingsMutation>);

    const { rerender } = customRender(<StepWithBusyProbe />);

    expect(screen.getByText("step busy")).toBeInTheDocument();

    mockedSettingsMutation.mockReturnValue({
      mutate,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    rerender(<StepWithBusyProbe />);

    expect(screen.getByText("step free")).toBeInTheDocument();
  });

  it("stays free while the connection test is pending", () => {
    setTestState({ isPending: true });

    customRender(<StepWithBusyProbe />);

    expect(screen.getByText("step free")).toBeInTheDocument();
  });

  it("is named for what it gives the reader and names the servers", () => {
    customRender(<SeerrStep onNext={onNext} />);

    expect(
      screen.getByRole("heading", { name: "Requests" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/connect overseerr or jellyseerr, the seerr family/i),
    ).toBeInTheDocument();
    // "Seerr" is a family of products, not one a newcomer runs by that name,
    // so it is never the label; the body names it once, so the reader can
    // find the Seerr tab in Settings, Connections.
    expect(
      screen.queryByRole("heading", { name: /seerr/i }),
    ).not.toBeInTheDocument();
  });

  it("points a failed save at the Seerr tab in Settings, Connections", async () => {
    const user = userEvent.setup();
    mutate.mockImplementationOnce(
      (_input: unknown, opts?: { onError?: () => void }) => opts?.onError?.(),
    );
    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");
    await user.click(
      screen.getByRole("button", { name: /save and continue anyway/i }),
    );

    expect(
      await screen.findByText(/later in settings, connections, seerr/i),
    ).toBeInTheDocument();
  });

  it("writes the connection and enables Seerr on Continue", async () => {
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledWith(
      {
        "settings-seerr-url": "http://seerr:5055",
        "settings-seerr-apikey": "seerr-key",
        "settings-seerr-verify_ssl": true,
        "settings-general-use_seerr": true,
      },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("Continue with nothing filled in writes no settings", async () => {
    const user = userEvent.setup();
    customRender(<SeerrStep onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /continue without requests/i }),
    );

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("tests the typed connection", async () => {
    const user = userEvent.setup();
    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");
    await user.click(screen.getByRole("button", { name: /^test$/i }));

    expect(testMutate).toHaveBeenCalledWith({
      url: "http://seerr:5055",
      apikey: "seerr-key",
      verifySsl: true,
    });
  });

  // The verdict belongs to the connection it was measured against: the hook
  // is handed the current values so it can drop a green result the moment the
  // URL or the key changes.
  it("hands the test hook the connection currently on screen", async () => {
    const user = userEvent.setup();
    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");

    expect(mockedTestMutation).toHaveBeenLastCalledWith(
      {
        url: "http://seerr:5055",
        apikey: "seerr-key",
        verifySsl: true,
      },
      // A failed Test is said beside the button, not in the client's toast.
      { inlineErrors: true },
    );
  });

  // The step is optional, so a connection nobody proved is still allowed
  // through. The label is what stops it going through silently.
  it("says what it is saving when the connection was never proved", async () => {
    const user = userEvent.setup();
    customRender(<SeerrStep onNext={onNext} />);

    expect(
      screen.getByRole("button", { name: /continue without requests/i }),
    ).toBeInTheDocument();

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");

    expect(
      screen.getByRole("button", { name: /save and continue anyway/i }),
    ).toBeInTheDocument();
  });

  it("reads plain Continue once the connection tested green", () => {
    setTestState({
      data: {
        success: true,
        application_title: "Jellyseerr",
        acting_user: {
          id: 1,
          display_name: "owner",
          can_request_movie: true,
          can_request_tv: true,
          can_request_4k_movie: false,
          can_request_4k_tv: false,
        },
      },
    });

    customRender(<SeerrStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /save and continue anyway/i }),
    ).not.toBeInTheDocument();
  });

  it("surfaces a failed save", async () => {
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onError?: (reason: unknown) => void }) => {
        opts?.onError?.(new Error("nope"));
      },
    );

    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/server url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(
      await screen.findByText(/could not save the request server/i),
    ).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("warns when the Seerr user cannot request both kinds", () => {
    setTestState({
      data: {
        success: true,
        application_title: "Jellyseerr",
        acting_user: {
          id: 1,
          display_name: "owner",
          can_request_movie: true,
          can_request_tv: false,
          can_request_4k_movie: false,
          can_request_4k_tv: false,
        },
      },
    });

    customRender(<SeerrStep onNext={onNext} />);

    expect(screen.getByText(/connected to jellyseerr/i)).toBeInTheDocument();
    expect(
      screen.getByText(/cannot request both movies and series/i),
    ).toBeInTheDocument();
    // Naming the problem without naming the fix left the reader with a
    // warning and nothing to do about it.
    expect(
      screen.getByText(/request movies and request series permissions/i),
    ).toBeInTheDocument();
  });

  it("renders no skip control of its own", () => {
    customRender(<SeerrStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });
});
