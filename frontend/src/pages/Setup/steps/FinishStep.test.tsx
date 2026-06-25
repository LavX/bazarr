import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useArrInstances,
  useLanguageProfiles,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import FinishStep from "./FinishStep";

// Navigation is asserted; mock react-router's useNavigate like the shell test.
const navigate = vi.fn();

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

// Keep the real barrel and override only the hooks this step reads/drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useArrInstances: vi.fn(),
    useLanguageProfiles: vi.fn(),
    useSystemSettings: vi.fn(),
    useSettingsMutation: vi.fn(),
  };
});

const mockedUseArrInstances = vi.mocked(useArrInstances);
const mockedUseLanguageProfiles = vi.mocked(useLanguageProfiles);
const mockedUseSystemSettings = vi.mocked(useSystemSettings);
const mockedUseSettingsMutation = vi.mocked(useSettingsMutation);

const mutate = vi.fn();

function setArrInstances(data: unknown) {
  mockedUseArrInstances.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useArrInstances>);
}

function setProfiles(data: unknown) {
  mockedUseLanguageProfiles.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useLanguageProfiles>);
}

function setGeneral(general: Partial<Settings.General>) {
  mockedUseSystemSettings.mockReturnValue({
    data: { general },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

describe("FinishStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setArrInstances([
      { id: 1, kind: "sonarr", name: "Main Sonarr" },
      { id: 2, kind: "radarr", name: "Main Radarr" },
    ]);
    setProfiles([{ name: "Default", profileId: 1 }]);
    setGeneral({
      use_plex: true,
      use_jellyfin: false,
      enabled_providers: ["opensubtitles"],
    });
    mockedUseSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("summarizes the configured state", () => {
    customRender(<FinishStep onNext={vi.fn()} />);

    // Sonarr / Radarr counts.
    expect(screen.getByText(/sonarr/i)).toBeInTheDocument();
    expect(screen.getByText(/radarr/i)).toBeInTheDocument();
    // A language profile was created.
    expect(screen.getByText(/language profile/i)).toBeInTheDocument();
    // An enabled provider shows up.
    expect(screen.getByText(/provider/i)).toBeInTheDocument();
    // Plex on, Jellyfin off.
    expect(screen.getByText(/plex/i)).toBeInTheDocument();
  });

  it("marks setup complete and navigates home on Finish", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /finish/i }));

    expect(mutate).toHaveBeenCalledWith(
      { "settings-general-setup_complete": true },
      expect.anything(),
    );

    expect(navigate).not.toHaveBeenCalled();
    onSuccess?.();
    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/"));
  });

  it("renders a Back button when onBack is provided", () => {
    customRender(<FinishStep onNext={vi.fn()} onBack={vi.fn()} />);

    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
  });
});
