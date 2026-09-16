import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { useSeerrTestConnectionMutation } from "@/apis/hooks/seerr";
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
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("writes the connection and enables Seerr on Continue", async () => {
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/seerr url/i), "http://seerr:5055");
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
      screen.getByRole("button", { name: /continue without seerr/i }),
    );

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("tests the typed connection", async () => {
    const user = userEvent.setup();
    customRender(<SeerrStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/seerr url/i), "http://seerr:5055");
    await user.type(screen.getByLabelText(/api key/i), "seerr-key");
    await user.click(screen.getByRole("button", { name: /^test$/i }));

    expect(testMutate).toHaveBeenCalledWith({
      url: "http://seerr:5055",
      apikey: "seerr-key",
      verifySsl: true,
    });
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
  });

  it("renders no skip control of its own", () => {
    customRender(<SeerrStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });
});
