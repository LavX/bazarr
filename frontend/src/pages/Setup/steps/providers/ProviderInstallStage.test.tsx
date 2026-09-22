import userEvent, { UserEvent } from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useSettingsMutation,
  useSystem,
  useSystemSettings,
} from "@/apis/hooks";
import api from "@/apis/raw";
import { customRender, fireEvent, screen, waitFor } from "@/tests";
import ProviderInstallStage from "./ProviderInstallStage";
import { redirectToSetup } from "./redirect";

// Keep the real barrel and override only the hooks this stage drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubCatalog: vi.fn(),
    useProviderHubInstall: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystem: vi.fn(),
    useSystemSettings: vi.fn(),
  };
});

// The post-restart health poll hits api.system.status() directly.
vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      status: vi.fn(),
    },
  },
}));

// The hard redirect lives in its own module so it is trivially mockable.
vi.mock("./redirect", () => ({
  redirectToSetup: vi.fn(),
}));

const mockedCatalog = vi.mocked(useProviderHubCatalog);
const mockedInstall = vi.mocked(useProviderHubInstall);
const mockedSettingsMutation = vi.mocked(useSettingsMutation);
const mockedSystemSettings = vi.mocked(useSystemSettings);
const mockedSystem = vi.mocked(useSystem);
const mockedStatus = vi.mocked(api.system.status);
const mockedRedirect = vi.mocked(redirectToSetup);

const mutateAsync = vi.fn();
const settingsMutateAsync = vi.fn();
const restart = vi.fn();
const onInstalledNeedsRestart = vi.fn();
const onUseInstalled = vi.fn();
const onNext = vi.fn();

const refetchCatalog = vi.fn();

function setCatalog(entries: unknown[]) {
  mockedCatalog.mockReturnValue({
    data: { sources: [], entries },
    // Settled: the stage reads this to tell an empty catalog from one that has
    // not answered yet.
    isPending: false,
    isError: false,
    error: null,
    isFetching: false,
    refetch: refetchCatalog,
  } as unknown as ReturnType<typeof useProviderHubCatalog>);
}

function setCatalogError(error: unknown) {
  mockedCatalog.mockReturnValue({
    data: undefined,
    isPending: false,
    isError: true,
    error,
    isFetching: false,
    refetch: refetchCatalog,
  } as unknown as ReturnType<typeof useProviderHubCatalog>);
}

function setEnabledProviders(ids: string[]) {
  mockedSystemSettings.mockReturnValue({
    data: { general: { enabled_providers: ids } },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

const opensubtitlesEntry = {
  provider_id: "opensubtitles",
  name: "OpenSubtitles",
  version: "1.0.0",
  trusted: true,
  manifest: { id: "opensubtitles", name: "OpenSubtitles" },
};

const subsceneEntry = {
  provider_id: "subscene",
  name: "Subscene",
  version: "1.0.0",
  trusted: true,
  manifest: { id: "subscene", name: "Subscene" },
};

const gestdownEntry = {
  provider_id: "gestdown",
  name: "Gestdown",
  version: "1.0.0",
  trusted: true,
  manifest: { id: "gestdown", name: "Gestdown" },
};

// Shaped like the real catalog entries: no credentials, nothing required, and
// no helper service, which is what the recommended set is read from. `id`
// carries the provider id as well, which is the key failOnly() rejects on.
function recommendedEntry(providerId: string, name: string) {
  return {
    provider_id: providerId,
    name,
    version: "1.0.0",
    trusted: true,
    manifest: {
      id: providerId,
      provider_id: providerId,
      name,
      config_schema: {
        type: "object",
        properties: { request_delay_ms: { type: "integer", default: 0 } },
      },
    },
  };
}

const subtitlecatEntry = recommendedEntry("subtitlecat", "SubtitleCat");
const tvsubtitlesEntry = recommendedEntry("tvsubtitles", "TVsubtitles");

// Not recommended: an API key is a signup, and it is required, so this one is
// only reachable by ticking it by hand.
const subdlEntry = {
  provider_id: "subdl",
  name: "SubDL",
  version: "1.0.0",
  trusted: true,
  manifest: {
    provider_id: "subdl",
    name: "SubDL",
    secret_fields: ["api_key"],
    config_schema: {
      type: "object",
      required: ["api_key"],
      properties: { api_key: { type: "string", secret: true } },
    },
  },
};

// Fails only the named providers, so a run can be set up to fail the first
// selection, the last, or all of them.
function failOnly(...names: string[]) {
  mutateAsync.mockImplementation(({ manifest }: { manifest: LooseObject }) =>
    names.includes(manifest.id as string)
      ? Promise.reject(new Error(`${manifest.id as string} is unavailable`))
      : Promise.resolve(undefined),
  );
}

// Promise.allSettled plus the state writes behind it take several microtask
// turns to reach the DOM, and a zero-length fake-timer advance only flushes
// one. waitFor is not an option here: under fake timers it advances the clock,
// which is the very thing these tests are measuring.
async function settleInstalls() {
  for (let i = 0; i < 10; i += 1) {
    await vi.advanceTimersByTimeAsync(0);
  }
}

/** One countdown second: move the clock, then let the re-render land. */
async function tick() {
  await vi.advanceTimersByTimeAsync(1000);
  await settleInstalls();
}

/** `seconds` countdown seconds, one at a time. Each tick schedules the next
 *  from an effect, so they cannot be collapsed into a single long advance. */
async function tickSeconds(seconds: number) {
  for (let i = 0; i < seconds; i += 1) {
    await tick();
  }
}

async function selectAndInstall(user: UserEvent, ...names: RegExp[]) {
  for (const name of names) {
    await user.click(screen.getByRole("checkbox", { name }));
  }
  await user.click(screen.getByRole("button", { name: /install & restart/i }));
}

describe("ProviderInstallStage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setCatalog([opensubtitlesEntry, subsceneEntry]);
    setEnabledProviders([]);
    mutateAsync.mockResolvedValue(undefined);
    settingsMutateAsync.mockResolvedValue(undefined);
    mockedInstall.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useProviderHubInstall>);
    mockedSettingsMutation.mockReturnValue({
      mutateAsync: settingsMutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    restart.mockImplementation((opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });
    mockedSystem.mockReturnValue({
      restart,
      isMutating: false,
    } as unknown as ReturnType<typeof useSystem>);
    mockedStatus.mockResolvedValue({} as never);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // A first-run install with no reachable catalog has nothing to select, and
  // the only other control on the step is disabled until something is. The
  // wizard has four more steps after this one, so a reader who cannot reach
  // the catalog was stranded two thirds of the way through it.
  it("recovers the step when the catalog offers nothing and nothing is installed", async () => {
    const user = userEvent.setup();
    setCatalog([]);
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(screen.getByText(/no providers available/i)).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /install & restart/i }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: /continue without providers/i }),
    );
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  // A catalog that could not be fetched and a catalog with nothing in it both
  // arrive as `data: undefined`. Reading them as the same thing told a reader
  // whose request failed that Bazarr+ has no providers, which is a different
  // problem with a different fix, and offered no way to try again.
  it("tells a catalog that failed from a catalog that is empty", async () => {
    const user = userEvent.setup();
    setCatalogError(new Error("provider catalog unreachable"));
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(
      screen.getByText(/could not load the provider catalog/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/provider catalog unreachable/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/no installable providers were found/i),
    ).toBeNull();

    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetchCatalog).toHaveBeenCalled();
  });

  it("names the search box for a screen reader", () => {
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(screen.getByLabelText("Search providers")).toBeInTheDocument();
  });

  // An answer that has not arrived is not an empty one. Reading the list while
  // the query was still in flight reported a catalog with nothing in it on a
  // healthy install, offered a recovery from a state it was not in, and then
  // replaced both a moment later.
  it("says nothing about the catalog while it is still being read", () => {
    mockedCatalog.mockReturnValue({
      data: undefined,
      isPending: true,
    } as unknown as ReturnType<typeof useProviderHubCatalog>);
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(screen.queryByText(/no providers available/i)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /continue without providers/i }),
    ).toBeNull();
  });

  // It is a recovery, not a way to decline the step: a reader who can see the
  // catalog is answering it, not stuck on it.
  it("offers no recovery when the catalog loaded normally", () => {
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(screen.queryByText(/no providers available/i)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /continue without providers/i }),
    ).toBeNull();
  });

  // Providers already on disk are their own way forward, offered above.
  it("offers no recovery when providers are already installed", () => {
    setCatalog([]);
    customRender(
      <ProviderInstallStage
        hasInstalled
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(screen.getByText(/no providers available/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /continue without providers/i }),
    ).toBeNull();
    expect(
      screen.getByRole("button", { name: /use already-installed providers/i }),
    ).toBeInTheDocument();
  });

  it("installs each selected provider, then restarts and shows the overlay", async () => {
    const user = userEvent.setup();
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    await user.click(screen.getByRole("checkbox", { name: /subscene/i }));
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(2);
    });
    expect(mutateAsync).toHaveBeenCalledWith({
      manifest: opensubtitlesEntry.manifest,
    });
    expect(mutateAsync).toHaveBeenCalledWith({
      manifest: subsceneEntry.manifest,
    });

    await waitFor(() => {
      expect(restart).toHaveBeenCalled();
    });
    expect(await screen.findByText(/restarting bazarr/i)).toBeInTheDocument();
    expect(
      screen.getByText(/finishing provider installation/i),
    ).toBeInTheDocument();
  });

  it("redirects to /setup once the post-restart health poll succeeds", async () => {
    vi.useFakeTimers();

    // First poll rejects (still down), second resolves (back up).
    mockedStatus
      .mockRejectedValueOnce(new Error("down"))
      .mockResolvedValueOnce({} as never);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    // fireEvent avoids userEvent's own timer interplay under fake timers.
    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));

    // Let installs (awaited mutateAsync) + restart settle, then start polling.
    await vi.advanceTimersByTimeAsync(0);
    expect(restart).toHaveBeenCalled();

    // Drive the poll interval until status resolves and the redirect fires.
    await vi.advanceTimersByTimeAsync(5000);
    await vi.advanceTimersByTimeAsync(5000);

    expect(mockedRedirect).toHaveBeenCalledTimes(1);
  });

  it("attempts every selection when the first one fails", async () => {
    // The loop this replaced awaited each install in turn with no catch: the
    // first rejection ended the run, the two behind it were never attempted,
    // and the unhandled rejection left the button sitting there saying nothing.
    const user = userEvent.setup();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("opensubtitles");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await selectAndInstall(user, /opensubtitles/i, /subscene/i, /gestdown/i);

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(3);
    });
    for (const entry of [opensubtitlesEntry, subsceneEntry, gestdownEntry]) {
      expect(mutateAsync).toHaveBeenCalledWith({ manifest: entry.manifest });
    }
  });

  it("names which providers installed and which failed, and why", async () => {
    const user = userEvent.setup();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await selectAndInstall(user, /opensubtitles/i, /subscene/i, /gestdown/i);

    expect(
      await screen.findByText(/some providers did not install/i),
    ).toBeInTheDocument();
    expect(screen.getByText("Subscene")).toBeInTheDocument();
    expect(screen.getByText(/subscene is unavailable/i)).toBeInTheDocument();
    expect(
      screen.getAllByText(/installed, waiting for the restart/i),
    ).toHaveLength(2);
  });

  it("restarts a partial run on its own once the countdown expires", async () => {
    // A staged provider that never activates is a worse state to leave someone
    // in than a restart they watched coming, so the restart is not optional.
    // The delay only buys time to read the failures and retry them.
    vi.useFakeTimers();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /subscene/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /gestdown/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await settleInstalls();

    expect(
      screen.getByText(/restarting in 10s to activate 2 of 3 providers/i),
    ).toBeInTheDocument();
    expect(restart).not.toHaveBeenCalled();

    await tick();
    expect(
      screen.getByText(/restarting in 9s to activate 2 of 3 providers/i),
    ).toBeInTheDocument();
    expect(restart).not.toHaveBeenCalled();

    await tickSeconds(9);
    expect(restart).toHaveBeenCalledTimes(1);
    expect(onInstalledNeedsRestart).toHaveBeenCalled();
    expect(screen.getByText(/restarting bazarr/i)).toBeInTheDocument();
  });

  it("announces the countdown twice, not once a second", async () => {
    // A polite live region that changes every second enqueues ten messages in
    // ten seconds and a screen reader needs three or four to read each, so the
    // queue would still be draining after the page had bounced. Two messages,
    // both naming the way out, and the ticking number says nothing.
    vi.useFakeTimers();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /subscene/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /gestdown/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await settleInstalls();

    const live = screen.getByRole("status");
    expect(live).toHaveTextContent(
      "Restarting in 10 seconds to activate 2 of 3 providers. Retry the failures to cancel.",
    );

    // The visible ticker is not the live region and announces nothing.
    const ticker = screen.getByText(
      /restarting in 10s to activate 2 of 3 providers/i,
    );
    expect(ticker).toHaveAttribute("aria-hidden", "true");
    expect(ticker).not.toBe(live);
    expect(live).not.toHaveTextContent(/10s/);

    // Six seconds of ticking, and the announcement has not moved.
    await tickSeconds(6);
    expect(
      screen.getByText(/restarting in 4s to activate 2 of 3 providers/i),
    ).toBeInTheDocument();
    expect(live).toHaveTextContent(
      "Restarting in 10 seconds to activate 2 of 3 providers. Retry the failures to cancel.",
    );

    // One last warning with three seconds left, then silence until the bounce.
    await tick();
    expect(live).toHaveTextContent(
      "Restarting in 3 seconds. Retry the failures to cancel.",
    );
    await tickSeconds(2);
    expect(live).toHaveTextContent(
      "Restarting in 3 seconds. Retry the failures to cancel.",
    );
  });

  it("stops announcing once the restart is cancelled", async () => {
    vi.useFakeTimers();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /subscene/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /gestdown/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await settleInstalls();
    expect(screen.getByRole("status")).toHaveTextContent(/restarting in 10/i);

    mutateAsync.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: /retry the failure/i }));
    await settleInstalls();

    expect(screen.getByRole("status")).toHaveTextContent("");
  });

  it("retrying the failures cancels the pending restart", async () => {
    vi.useFakeTimers();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /subscene/i }));
    fireEvent.click(screen.getByRole("checkbox", { name: /gestdown/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await settleInstalls();
    expect(screen.getByText(/restarting in 10s/i)).toBeInTheDocument();

    // A retry that is still in flight. The countdown must not run out under it.
    mutateAsync.mockImplementation(() => new Promise(() => {}));
    fireEvent.click(screen.getByRole("button", { name: /retry the failure/i }));
    await settleInstalls();

    expect(screen.queryByText(/restarting in/i)).not.toBeInTheDocument();

    await tickSeconds(30);
    expect(restart).not.toHaveBeenCalled();
  });

  it("Restart now skips the countdown", async () => {
    const user = userEvent.setup();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await selectAndInstall(user, /opensubtitles/i, /subscene/i, /gestdown/i);
    await screen.findByText(/restarting in 10s/i);

    await user.click(screen.getByRole("button", { name: /restart now/i }));

    expect(restart).toHaveBeenCalledTimes(1);
    expect(await screen.findByText(/restarting bazarr/i)).toBeInTheDocument();
  });

  it("does not restart when nothing staged", async () => {
    const user = userEvent.setup();
    setCatalog([opensubtitlesEntry, subsceneEntry]);
    failOnly("opensubtitles", "subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await selectAndInstall(user, /opensubtitles/i, /subscene/i);

    expect(
      await screen.findByText(/no provider installed/i),
    ).toBeInTheDocument();
    expect(restart).not.toHaveBeenCalled();
    expect(onInstalledNeedsRestart).not.toHaveBeenCalled();
    expect(screen.queryByText(/restarting bazarr/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/restarting in/i)).not.toBeInTheDocument();
  });

  it("retries only the providers that failed", async () => {
    const user = userEvent.setup();
    setCatalog([opensubtitlesEntry, subsceneEntry, gestdownEntry]);
    failOnly("subscene");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await selectAndInstall(user, /opensubtitles/i, /subscene/i, /gestdown/i);
    await screen.findByText(/subscene is unavailable/i);

    mutateAsync.mockClear();
    failOnly();
    await user.click(
      screen.getByRole("button", { name: /retry the failure/i }),
    );

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(1);
    });
    expect(mutateAsync).toHaveBeenCalledWith({
      manifest: subsceneEntry.manifest,
    });
    // Nothing is left failing, so the run finishes the way a clean one does.
    expect(await screen.findByText(/restarting bazarr/i)).toBeInTheDocument();
    expect(restart).toHaveBeenCalled();
  });

  it("offers Use already-installed providers only when hasInstalled is true", async () => {
    const user = userEvent.setup();
    const { unmount } = customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(
      screen.queryByRole("button", { name: /use already-installed/i }),
    ).not.toBeInTheDocument();

    unmount();

    customRender(
      <ProviderInstallStage
        hasInstalled
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /use already-installed/i }),
    );

    expect(onUseInstalled).toHaveBeenCalled();
  });

  // The ask was one action that installs and enables everything that needs no
  // account and no extra service, so that a reader who does not know which of
  // sixty providers will just work is not left to guess.
  it("installs the recommended set, enables it, then restarts", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry, tvsubtitlesEntry, subdlEntry]);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    // What the set means, and how many it is, are both readable before the
    // click: the count is the button, the reason is the line under it.
    const button = screen.getByRole("button", {
      name: /install 2 recommended providers/i,
    });
    const hint = screen.getByText(/no account and no extra service/i);
    expect(button).toHaveAttribute("aria-describedby", hint.id);

    await user.click(button);

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(2);
    });
    expect(mutateAsync).toHaveBeenCalledWith({
      manifest: subtitlecatEntry.manifest,
    });
    expect(mutateAsync).toHaveBeenCalledWith({
      manifest: tvsubtitlesEntry.manifest,
    });
    // SubDL was never attempted: it asks for an API key.
    expect(mutateAsync).not.toHaveBeenCalledWith({
      manifest: subdlEntry.manifest,
    });

    // Enabled before the restart, never after: the page is gone by then.
    await waitFor(() => {
      expect(settingsMutateAsync).toHaveBeenCalledWith({
        "settings-general-enabled_providers": ["subtitlecat", "tvsubtitles"],
      });
    });
    await waitFor(() => {
      expect(restart).toHaveBeenCalled();
    });
  });

  it("keeps the providers that were already enabled", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry]);
    setEnabledProviders(["opensubtitlescom"]);

    customRender(
      <ProviderInstallStage
        hasInstalled
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /install 1 recommended provider/i }),
    );

    await waitFor(() => {
      expect(settingsMutateAsync).toHaveBeenCalledWith({
        "settings-general-enabled_providers": [
          "opensubtitlescom",
          "subtitlecat",
        ],
      });
    });
  });

  it("reads the recommended set off the catalog, not off the search box", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry, tvsubtitlesEntry, subdlEntry]);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.type(screen.getByPlaceholderText(/search providers/i), "subdl");

    // The list narrows; what the button offers does not.
    expect(
      screen.getByRole("checkbox", { name: /subdl/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /install 2 recommended providers/i }),
    ).toBeInTheDocument();
  });

  it("enables only what staged when part of a recommended install fails", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry, tvsubtitlesEntry]);
    failOnly("tvsubtitles");

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /install 2 recommended providers/i }),
    );

    await waitFor(() => {
      expect(settingsMutateAsync).toHaveBeenCalledWith({
        "settings-general-enabled_providers": ["subtitlecat"],
      });
    });
    // The same per-provider report the hand-picked run gives.
    expect(
      await screen.findByText(/some providers did not install/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/tvsubtitles is unavailable/i)).toBeInTheDocument();
    expect(
      screen.getByText(/installed, waiting for the restart/i),
    ).toBeInTheDocument();
  });

  it("reports an enable that failed, and restarts anyway", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry]);
    settingsMutateAsync.mockRejectedValue(new Error("config write failed"));

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /install 1 recommended provider/i }),
    );

    expect(
      await screen.findByText(/installed, but not enabled/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/config write failed/i)).toBeInTheDocument();
    // Staged providers still load: the enable can be fixed on the next screen.
    await waitFor(() => {
      expect(restart).toHaveBeenCalled();
    });
  });

  // Enabling is a union with what is already enabled, so a list that cannot be
  // read is a list that must not be written: replacing it would turn off
  // providers the reader had already chosen.
  it("leaves the enabled list alone when it cannot be read", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry]);
    mockedSystemSettings.mockReturnValue({
      data: undefined,
    } as unknown as ReturnType<typeof useSystemSettings>);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: /install 1 recommended provider/i }),
    );

    expect(
      await screen.findByText(/installed, but not enabled/i),
    ).toBeInTheDocument();
    expect(settingsMutateAsync).not.toHaveBeenCalled();
  });

  it("installs and nothing more when providers are picked by hand", async () => {
    const user = userEvent.setup();
    setCatalog([subtitlecatEntry, subdlEntry]);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /subtitlecat/i }));
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );

    await waitFor(() => {
      expect(restart).toHaveBeenCalled();
    });
    expect(settingsMutateAsync).not.toHaveBeenCalled();
  });

  it("offers no recommended action when nothing in the catalog qualifies", () => {
    setCatalog([subdlEntry]);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    expect(
      screen.queryByRole("button", { name: /recommended/i }),
    ).not.toBeInTheDocument();
    // The step is still answerable by hand, exactly as before.
    expect(
      screen.getByRole("checkbox", { name: /subdl/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /install & restart/i }),
    ).toBeInTheDocument();
  });
});
