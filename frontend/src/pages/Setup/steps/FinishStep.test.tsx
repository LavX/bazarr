import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useArrInstances,
  useLanguageProfiles,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import {
  ConnectionTest,
  recordConnectionTest,
} from "@/pages/Setup/connectionTests";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import FinishStep from "./FinishStep";

// The recap reads the media server rows, not the master switches. Staging them
// per kind is what proves a connected Emby is named instead of being reported
// as "Plex skipped, Jellyfin skipped". Each row is recorded as having passed
// its Test unless a test says otherwise, because only then may the recap call
// it connected.
function setMediaServers(
  rows: Record<string, string[]>,
  off: string[] = [],
  tested: ConnectionTest = "passed",
) {
  for (const [kind, names] of Object.entries(rows)) {
    names.forEach((_name, index) =>
      recordConnectionTest(`media-server:${kind}-${index}`, tested),
    );
  }
  server.use(
    http.get("/api/system/media-server-instances", ({ request }) => {
      const kind = new URL(request.url).searchParams.get("kind") ?? "";
      return HttpResponse.json({
        data: (rows[kind] ?? []).map((name, index) => ({
          id: `${kind}-${index}`,
          kind,
          name,
          enabled: !off.includes(kind),
          url: "http://10.0.0.9:8096",
          verify_ssl: true,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        })),
      });
    }),
  );
}

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

// The four media server master switches always exist in the real settings, and
// the recap reads them: the dispatcher checks use_<kind> before it looks at any
// row, so a saved server whose switch never landed refreshes nothing. They
// default on here and a test says otherwise when that is the case under test.
function setGeneral(
  general: Partial<Settings.General>,
  translator: Partial<Settings.Translator> = {},
) {
  mockedUseSystemSettings.mockReturnValue({
    data: {
      general: {
        // eslint-disable-next-line camelcase
        use_plex: true,
        // eslint-disable-next-line camelcase
        use_jellyfin: true,
        // eslint-disable-next-line camelcase
        use_emby: true,
        // eslint-disable-next-line camelcase
        use_silo: true,
        ...general,
      },
      translator,
    },
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

  it("summarizes the configured state", async () => {
    setMediaServers({ plex: ["Plex"] });
    customRender(<FinishStep onNext={vi.fn()} />);

    // Sonarr / Radarr counts.
    expect(screen.getByText(/sonarr/i)).toBeInTheDocument();
    expect(screen.getByText(/radarr/i)).toBeInTheDocument();
    // A language profile was created.
    expect(screen.getByText(/language profile/i)).toBeInTheDocument();
    // An enabled provider shows up.
    expect(screen.getByText(/provider/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/plex connected \(1 server\)/i),
    ).toBeInTheDocument();
  });

  it("names every kind of media server, not just Plex and Jellyfin", async () => {
    // use_plex and use_jellyfin were the only switches the recap read, so a
    // reader who connected Emby was told "Plex skipped, Jellyfin skipped" and
    // never saw the server they had actually set up.
    setMediaServers({ emby: ["Emby", "Emby 2"], silo: ["Silo"] });
    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      await screen.findByText(/emby connected \(2 servers\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/silo connected \(1 server\)/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/plex media server skipped/i)).toBeNull();
  });

  it("does not count a switched-off row as a connected server", async () => {
    // Signing out of Plex leaves the destination row behind, switched off and
    // stripped of its credential, because signing back in should keep the
    // libraries the reader chose. The dispatcher skips it, but the recap read
    // the row and reported Plex connected, so the reader finished setup
    // believing that server was a refresh destination.
    setMediaServers({ plex: ["Plex"], emby: ["Emby"] }, ["plex"]);
    customRender(<FinishStep onNext={vi.fn()} />);

    // The switched-on server lands first, which is what says the rows have
    // arrived and the recap is looking at them.
    expect(
      await screen.findByText(/emby connected \(1 server\)/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/plex connected/i)).toBeNull();
  });

  it("does not call a server connected whose kind is switched off", async () => {
    // "Continue anyway" on a configure step whose master switch write failed
    // leaves an enabled row under a use_<kind> that is still false. The
    // dispatcher reads that switch before any row, so nothing refreshes at
    // all; the recap counted the row, reported the server connected and
    // dropped the line saying where to finish the job.
    setMediaServers({ emby: ["Emby"], silo: ["Silo"] });
    setGeneral({
      // eslint-disable-next-line camelcase
      use_emby: false,
      // eslint-disable-next-line camelcase
      enabled_providers: ["opensubtitles"],
    });
    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      await screen.findByText(/silo connected \(1 server\)/i),
    ).toBeInTheDocument();
    expect(screen.queryByText(/emby connected/i)).toBeNull();
  });

  it("says so when the finish write fails instead of going quiet", async () => {
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onError?: () => void }) => {
        opts?.onError?.();
      },
    );

    customRender(<FinishStep onNext={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /finish/i }));

    expect(
      await screen.findByText(/could not finish setup/i),
    ).toBeInTheDocument();
    expect(navigate).not.toHaveBeenCalled();
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

    // No line about a step this user was never shown. Seerr is on both paths,
    // so it stays.
    expect(screen.queryByText(/sonarr/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/plex media server/i)).not.toBeInTheDocument();
    expect(screen.getByText(/seerr skipped/i)).toBeInTheDocument();
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

  it("counts a configured translator as done", async () => {
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    // A media server as well, because the picker is on this path too: with
    // none connected the recap has something left for later to report.
    setMediaServers({ jellyfin: ["Jellyfin"] });
    setGeneral(
      { enabled_providers: ["opensubtitles"], use_seerr: true },
      { openrouter_api_key: "sk-or-xyz" },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(screen.getByText(/ai translation configured/i)).toBeInTheDocument();
    expect(
      await screen.findByText(/jellyfin connected \(1 server\)/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/what you left for later/i),
    ).not.toBeInTheDocument();
  });

  it("names the media server a Discover reader connected", async () => {
    // The picker moved onto the Discover path, but its summary stayed in the
    // library-only half of the recap, so this reader finished setup with no
    // mention of the server they had just connected.
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    setArrInstances([]);
    setMediaServers({ emby: ["Living room"] });

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      await screen.findByText(/emby connected \(1 server\)/i),
    ).toBeInTheDocument();
    // Still no line about a step this reader was never shown.
    expect(screen.queryByText(/sonarr/i)).not.toBeInTheDocument();
  });

  // Saving never waited for a Test, so a wrong address or key came out of the
  // wizard reported as "Sonarr connected".
  it("calls an arr connected only when a Test passed against it", () => {
    localStorage.setItem("bazarr.onboarding.intent", "library");
    setArrInstances([
      { id: 1, kind: "sonarr", name: "Main Sonarr" },
      { id: 2, kind: "radarr", name: "Main Radarr" },
      { id: 3, kind: "sportarr", name: "Main Sportarr" },
    ]);
    recordConnectionTest("arr:1", "passed");
    recordConnectionTest("arr:2", "failed");

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      screen.getByText("Sonarr connected (1 instance)"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Radarr saved (1 instance), but the connection test failed",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Sportarr saved (1 instance), connection not tested"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/radarr connected/i)).toBeNull();
    expect(screen.queryByText(/sportarr connected/i)).toBeNull();
  });

  it("calls a media server connected only when a Test passed against it", async () => {
    setMediaServers({ silo: ["Silo"], emby: ["Emby"] }, [], "untested");
    recordConnectionTest("media-server:emby-0", "failed");

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      await screen.findByText("Silo saved (1 server), connection not tested"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Emby saved (1 server), but the connection test failed"),
    ).toBeInTheDocument();
    expect(screen.queryByText(/silo connected/i)).toBeNull();
    expect(screen.queryByText(/emby connected/i)).toBeNull();
  });

  it("forgets the connection test results on the way out", async () => {
    const user = userEvent.setup();
    recordConnectionTest("arr:1", "passed");
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<FinishStep onNext={vi.fn()} />);

    await user.click(screen.getByRole("button", { name: /finish/i }));
    onSuccess?.();

    await waitFor(() =>
      expect(
        localStorage.getItem("bazarr.onboarding.connection-tests"),
      ).toBeNull(),
    );
  });

  it("tells a Discover reader who connected nothing that nothing refreshes", async () => {
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    setArrInstances([]);
    setMediaServers({});

    customRender(<FinishStep onNext={vi.fn()} />);

    expect(
      await screen.findByText(/no media server connected/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /no media server is connected, so nothing is refreshed/i,
      ),
    ).toBeInTheDocument();
  });
});
