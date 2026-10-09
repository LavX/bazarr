import userEvent, { UserEvent } from "@testing-library/user-event";
import { AxiosError } from "axios";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useProviderHubRefreshCatalog,
  useSettingsMutation,
  useSystem,
  useSystemSettings,
} from "@/apis/hooks";
import api from "@/apis/raw";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import {
  act,
  customRender,
  fireEvent,
  screen,
  waitFor,
  within,
} from "@/tests";
import ProviderInstallStage from "./ProviderInstallStage";
import { redirectToSetup } from "./redirect";

// Keep the real barrel and override only the hooks this stage drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubCatalog: vi.fn(),
    useProviderHubInstall: vi.fn(),
    useProviderHubRefreshCatalog: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystem: vi.fn(),
    useSystemSettings: vi.fn(),
  };
});

// The post-restart health poll hits api.system.status() directly, and the
// password confirm modal posts through api.system.confirmPassword.
const mockConfirmPassword = vi.fn();
vi.mock("@/apis/raw", () => ({
  default: {
    system: {
      status: vi.fn(),
      confirmPassword: (...args: unknown[]) => mockConfirmPassword(...args),
    },
  },
}));

// The hard redirect lives in its own module so it is trivially mockable.
vi.mock("./redirect", () => ({
  redirectToSetup: vi.fn(),
}));

const mockedCatalog = vi.mocked(useProviderHubCatalog);
const mockedInstall = vi.mocked(useProviderHubInstall);
const mockedRefreshCatalog = vi.mocked(useProviderHubRefreshCatalog);
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
const refreshCatalogAsync = vi.fn();
const refetchSettings = vi.fn();

function setCatalog(entries: unknown[], sources: unknown[] = []) {
  mockedCatalog.mockReturnValue({
    data: { sources, entries },
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

// The interactive wizard can be reopened with form login configured (Settings
// > General > Run setup again), so the install batch runs through the same
// password confirmation the Hub does.
function setFormLogin() {
  mockedSystemSettings.mockReturnValue({
    data: {
      general: { enabled_providers: [] },
      auth: { type: "form", username: "admin" },
    },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

// The settings read has not answered yet, so nothing about auth is known.
// The wizard sits outside the app's settings-loading guard, and the catalog
// can arrive first, which is the ordering the gate must not read as login
// being off.
function setUnresolvedSettings() {
  mockedSystemSettings.mockReturnValue({
    data: undefined,
    isPending: true,
    isError: false,
    error: null,
    refetch: refetchSettings,
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
    refreshCatalogAsync.mockResolvedValue(undefined);
    mockedRefreshCatalog.mockReturnValue({
      mutateAsync: refreshCatalogAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useProviderHubRefreshCatalog>);
    mockedInstall.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useProviderHubInstall>);
    mockedSettingsMutation.mockReturnValue({
      mutateAsync: settingsMutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    // The restart is answered, as the hook's mutate(vars, options) does.
    restart.mockImplementation(
      (_vars: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );
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

  it("treats an empty catalog whose source failed to refresh as a failure", async () => {
    // The catalog answers 200 with nothing when its only source could not be
    // read, which is what a transient GitHub failure on the first boot after
    // an install looks like. That is a broken catalog, not an empty one, and
    // the reader was shown "No providers available" with nothing to press.
    const user = userEvent.setup();
    setCatalog(
      [],
      [
        {
          name: "bazarr-provider-catalog",
          url: "https://example.invalid/catalog",
          trusted: true,
          last_error: "GitHub returned 503",
        },
      ],
    );
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
    expect(screen.getByText(/github returned 503/i)).toBeInTheDocument();
    expect(screen.queryByText(/no providers available/i)).toBeNull();

    // Reading the catalog again would hand back the same saved error: the
    // backend remembers the attempt and does not go back out for the source
    // on a plain read. Retry asks for a refresh instead.
    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refreshCatalogAsync).toHaveBeenCalled();
    expect(refetchCatalog).not.toHaveBeenCalled();
  });

  it("falls back to re-reading the catalog when the refresh will not go through", async () => {
    const user = userEvent.setup();
    refreshCatalogAsync.mockRejectedValue(new Error("refresh refused"));
    setCatalog(
      [],
      [
        {
          name: "bazarr-provider-catalog",
          url: "https://example.invalid/catalog",
          trusted: true,
          last_error: "GitHub returned 503",
        },
      ],
    );
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(screen.getByRole("button", { name: /retry/i }));
    await waitFor(() => {
      expect(refetchCatalog).toHaveBeenCalled();
    });
    // The refresh's own failure is said, not swallowed for the re-read, and
    // the client's toast for it is off because it is said here.
    expect(mockedRefreshCatalog).toHaveBeenCalledWith({ inlineErrors: true });
    expect(
      screen.getByRole("alert", {
        name: /could not load the provider catalog/i,
      }),
    ).toHaveTextContent(
      "The retry could not refresh the catalog either: refresh refused",
    );
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

  function restartAnswers(error: AxiosError) {
    restart.mockImplementation(
      (_vars: unknown, opts?: { onError?: (error: unknown) => void }) => {
        opts?.onError?.(error);
      },
    );
  }

  it("says so when the backend refuses the restart, and does not reload", async () => {
    // The restart request had no error handling: the overlay and the health
    // poll started anyway, and the poll, finding the backend that had just
    // refused still up, reloaded into setup as if the restart had happened.
    vi.useFakeTimers();
    restartAnswers(
      new AxiosError("refused", "ERR_BAD_RESPONSE", undefined, undefined, {
        status: 500,
        statusText: "",
        headers: {},
        config: {} as never,
        data: { message: "Restart is not allowed on this install" },
      }),
    );

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );
    expect(mockedSystem).toHaveBeenCalledWith({ inlineErrors: true });
    expect(mockedInstall).toHaveBeenCalledWith({ inlineErrors: true });

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(restart).toHaveBeenCalledTimes(1);

    const alert = screen.getByRole("alert", { name: /did not restart/i });
    expect(alert).toHaveTextContent("Restart is not allowed on this install");
    expect(screen.queryByText(/restarting bazarr/i)).not.toBeInTheDocument();

    await vi.advanceTimersByTimeAsync(15_000);
    expect(mockedRedirect).not.toHaveBeenCalled();

    // The installs are staged, so the restart can be asked for again.
    fireEvent.click(screen.getByRole("button", { name: /restart now/i }));
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(restart).toHaveBeenCalledTimes(2);
  });

  it("ignores a health answer already on its way when the restart is refused", async () => {
    // Clearing the poll left a status request in flight; when it landed, from
    // the backend that had just refused and was still up, it reloaded into
    // setup as if the restart had happened.
    vi.useFakeTimers();
    let refuse: (() => void) | undefined;
    restart.mockImplementation(
      (_vars: unknown, opts?: { onError?: (error: unknown) => void }) => {
        refuse = () =>
          opts?.onError?.(
            new AxiosError(
              "refused",
              "ERR_BAD_RESPONSE",
              undefined,
              undefined,
              {
                status: 500,
                statusText: "",
                headers: {},
                config: {} as never,
                data: { message: "Restart is not allowed on this install" },
              },
            ),
          );
      },
    );
    let answerStatus: (() => void) | undefined;
    mockedStatus.mockImplementation(
      () =>
        new Promise((resolve) => {
          answerStatus = () => resolve({} as never);
        }),
    );

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await act(() => vi.advanceTimersByTimeAsync(100));
    expect(restart).toHaveBeenCalledTimes(1);

    // The poll's first status request goes out and has not answered yet.
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(mockedStatus).toHaveBeenCalledTimes(1);

    // The restart is refused, and only then does that request answer.
    await act(async () => refuse?.());
    await act(async () => answerStatus?.());
    await act(() => vi.advanceTimersByTimeAsync(15_000));

    expect(mockedRedirect).not.toHaveBeenCalled();
    expect(mockedStatus).toHaveBeenCalledTimes(1);
    expect(
      screen.getByRole("alert", { name: /did not restart/i }),
    ).toBeInTheDocument();
  });

  it("does not take a backend that answers before the restart is answered as restarted", async () => {
    vi.useFakeTimers();
    let accept: (() => void) | undefined;
    restart.mockImplementation(
      (_vars: unknown, opts?: { onSuccess?: () => void }) => {
        accept = () => opts?.onSuccess?.();
      },
    );
    mockedStatus.mockResolvedValue({} as never);

    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );
    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await act(() => vi.advanceTimersByTimeAsync(10_100));

    // Up and answering, but the restart has not been taken yet.
    expect(mockedStatus).toHaveBeenCalled();
    expect(mockedRedirect).not.toHaveBeenCalled();

    await act(async () => accept?.());
    await act(() => vi.advanceTimersByTimeAsync(5000));
    expect(mockedRedirect).toHaveBeenCalledTimes(1);
  });

  it("keeps waiting when the restart cuts the connection, as a restart does", async () => {
    vi.useFakeTimers();
    restartAnswers(new AxiosError("Network Error", "ERR_NETWORK"));
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

    fireEvent.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    fireEvent.click(screen.getByRole("button", { name: /install & restart/i }));
    await act(() => vi.advanceTimersByTimeAsync(100));

    expect(screen.getByText(/restarting bazarr/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("alert", { name: /did not restart/i }),
    ).not.toBeInTheDocument();
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
    // The settings answer, but the enabled list is not in the answer. The gate
    // has what it needs to run the batch, while the union still cannot write
    // a list it never saw. A read that never answers never gets this far any
    // more: the gate blocks the batch and asks for a retry instead.
    mockedSystemSettings.mockReturnValue({
      data: { general: {} },
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

describe("ProviderInstallStage and the password confirmation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setCatalog([opensubtitlesEntry, subsceneEntry]);
    setFormLogin();
    forgetPasswordConfirmation();
    mutateAsync.mockResolvedValue(undefined);
    settingsMutateAsync.mockResolvedValue(undefined);
    mockConfirmPassword.mockResolvedValue(undefined);
    mockedInstall.mockReturnValue({
      mutateAsync,
      isPending: false,
    } as unknown as ReturnType<typeof useProviderHubInstall>);
    restart.mockImplementation((opts?: { onSuccess?: () => void }) => {
      opts?.onSuccess?.();
    });
    mockedSystem.mockReturnValue({
      restart,
      isMutating: false,
    } as unknown as ReturnType<typeof useSystem>);
  });

  it("asks once for the whole selection, then installs it", async () => {
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

    // One prompt holds the whole batch: nothing installed, nothing confirmed.
    expect(mutateAsync).not.toHaveBeenCalled();
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.type(within(prompt).getByLabelText("Password"), "hunter2");
    await user.click(
      within(prompt).getByRole("button", { name: /^confirm$/i }),
    );

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(2);
    });
    expect(mockConfirmPassword).toHaveBeenCalledTimes(1);

    await waitFor(() => {
      expect(restart).toHaveBeenCalled();
    });
  });

  it("installs nothing when the confirmation is cancelled", async () => {
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
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );

    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.click(within(prompt).getByRole("button", { name: /cancel/i }));

    await waitFor(() => {
      expect(
        screen.queryByText("Confirm your password"),
      ).not.toBeInTheDocument();
    });
    expect(mutateAsync).not.toHaveBeenCalled();
  });

  it("waits for the auth settings to answer before running the batch", async () => {
    const user = userEvent.setup();
    setUnresolvedSettings();
    // The settings answer only once the gate asks for them, long after the
    // catalog did: a batch that read the unread settings as login off would
    // install without a prompt and land on a 403.
    refetchSettings.mockImplementation(async () => {
      setFormLogin();
      return {
        data: { auth: { type: "form", username: "admin" } },
      };
    });
    customRender(
      <ProviderInstallStage
        hasInstalled={false}
        onInstalledNeedsRestart={onInstalledNeedsRestart}
        onUseInstalled={onUseInstalled}
        onNext={onNext}
      />,
    );

    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    await user.click(
      screen.getByRole("button", { name: /install & restart/i }),
    );

    // Nothing ran while the settings were unread, and the read was retried
    // rather than answered from nothing.
    expect(mutateAsync).not.toHaveBeenCalled();
    expect(refetchSettings).toHaveBeenCalledTimes(1);
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.type(within(prompt).getByLabelText("Password"), "hunter2");
    await user.click(
      within(prompt).getByRole("button", { name: /^confirm$/i }),
    );

    await waitFor(() => {
      expect(mutateAsync).toHaveBeenCalledTimes(1);
    });
    expect(mockConfirmPassword).toHaveBeenCalledWith("admin", "hunter2");
  });
});
