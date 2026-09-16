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

function setGeneral(
  general: Partial<Settings.General>,
  translator: Partial<Settings.Translator> = {},
) {
  mockedUseSystemSettings.mockReturnValue({
    data: { general, translator },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

describe("FinishStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
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

  it("lands a Discover user on /discover and never mentions an arr", async () => {
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    setArrInstances([]);
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    // No line about a step this user was never shown.
    expect(screen.queryByText(/sonarr/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/plex media server/i)).not.toBeInTheDocument();
    expect(
      screen.getByText(/search for any film or series/i),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /finish and open discover/i }),
    );
    onSuccess?.();

    await waitFor(() => expect(navigate).toHaveBeenCalledWith("/discover"));
  });

  it("tells a library user their first scan is running", () => {
    localStorage.setItem("bazarr.onboarding.intent", "library");

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      screen.getByText(/first library scan starts now/i),
    ).toBeInTheDocument();
  });

  it("lists what was skipped and where it lives", () => {
    localStorage.setItem("bazarr.onboarding.intent", "library");
    setArrInstances([]);
    setGeneral({
      use_plex: false,
      use_jellyfin: false,
      enabled_providers: ["opensubtitles"],
    });

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(screen.getByText(/what you left for later/i)).toBeInTheDocument();
    expect(
      screen.getByText(/no sonarr, radarr or sportarr instance is connected/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/ai translation has no api key yet/i),
    ).toBeInTheDocument();
  });

  it("forgets the stored intent on the way out", async () => {
    const user = userEvent.setup();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    await user.click(
      screen.getByRole("button", { name: /finish and open discover/i }),
    );
    onSuccess?.();

    await waitFor(() =>
      expect(localStorage.getItem("bazarr.onboarding.intent")).toBeNull(),
    );
  });

  it("counts a configured translator as done", () => {
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    setGeneral(
      { enabled_providers: ["opensubtitles"] },
      { openrouter_api_key: "sk-or-xyz" },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(screen.getByText(/ai translation configured/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/what you left for later/i),
    ).not.toBeInTheDocument();
  });
});
