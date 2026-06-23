import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useArrInstances,
  useCreateArrInstance,
  useSettingsMutation,
  useTestArrInstanceConnection,
} from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import ArrStep from "./ArrStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the hooks the step drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useArrInstances: vi.fn(),
    useCreateArrInstance: vi.fn(),
    useTestArrInstanceConnection: vi.fn(),
    useSettingsMutation: vi.fn(),
  };
});

const mockedUseArrInstances = vi.mocked(useArrInstances);
const mockedUseCreateArrInstance = vi.mocked(useCreateArrInstance);
const mockedUseTestArrInstanceConnection = vi.mocked(
  useTestArrInstanceConnection,
);
const mockedUseSettingsMutation = vi.mocked(useSettingsMutation);

const onNext = vi.fn();
const createMutate = vi.fn();
const testMutate = vi.fn();
const settingsMutate = vi.fn();

function setInstances(data: unknown) {
  mockedUseArrInstances.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useArrInstances>);
}

function setTestState(state: Record<string, unknown>) {
  mockedUseTestArrInstanceConnection.mockReturnValue({
    mutate: testMutate,
    reset: vi.fn(),
    isPending: false,
    isError: false,
    isSuccess: false,
    data: undefined,
    ...state,
  } as unknown as ReturnType<typeof useTestArrInstanceConnection>);
}

describe("ArrStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setInstances([]);
    setTestState({});
    mockedUseCreateArrInstance.mockReturnValue({
      mutate: createMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useCreateArrInstance>);
    mockedUseSettingsMutation.mockReturnValue({
      mutate: settingsMutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("renders bespoke connection fields for the kind", () => {
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    expect(
      screen.getByRole("heading", { name: /sonarr/i }),
    ).toBeInTheDocument();
    expect(screen.getByLabelText(/name/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/address/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/port/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/api key/i)).toBeInTheDocument();
  });

  it("tests the connection with the entered values and shows the result", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);

    await user.type(screen.getByLabelText(/address/i), "10.0.0.5");
    await user.type(screen.getByLabelText(/api key/i), "abc123");
    await user.click(screen.getByRole("button", { name: /test/i }));

    expect(testMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        kind: "sonarr",
        ip: "10.0.0.5",
        api_key: "abc123",
        port: 8989,
      }),
    );

    // A successful result is surfaced inline.
    setTestState({
      data: { ok: true, app_name: "Sonarr", version: "4.0.0" },
      isSuccess: true,
    });
    customRender(<ArrStep kind="sonarr" onNext={onNext} />);
    expect(screen.getByText(/4\.0\.0/)).toBeInTheDocument();
  });

  it("creates the instance, enables use_sonarr, and advances on Continue", async () => {
    const user = userEvent.setup();
    createMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(<ArrStep kind="sonarr" required onNext={onNext} />);

    await user.clear(screen.getByLabelText(/name/i));
    await user.type(screen.getByLabelText(/name/i), "Main Sonarr");
    await user.type(screen.getByLabelText(/address/i), "10.0.0.5");
    await user.type(screen.getByLabelText(/api key/i), "abc123");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => {
      expect(createMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "sonarr",
          name: "Main Sonarr",
          ip: "10.0.0.5",
          api_key: "abc123",
          port: 8989,
        }),
        expect.anything(),
      );
    });

    expect(settingsMutate).toHaveBeenCalledWith({
      "settings-general-use_sonarr": true,
    });
    expect(onNext).toHaveBeenCalled();
  });

  it("shows a connected state for a pre-existing instance and does not create", async () => {
    const user = userEvent.setup();
    setInstances([{ id: 1, kind: "sonarr", name: "Existing Sonarr" }]);

    customRender(<ArrStep kind="sonarr" required onNext={onNext} />);

    expect(screen.getByText(/already connected/i)).toBeInTheDocument();
    expect(screen.getByText(/existing sonarr/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(createMutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("offers a Skip affordance when not required", async () => {
    const user = userEvent.setup();
    customRender(<ArrStep kind="radarr" onNext={onNext} />);

    await user.click(screen.getByRole("button", { name: /skip/i }));

    expect(createMutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });
});
