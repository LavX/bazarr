import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useSystem,
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
    useSystem: vi.fn(),
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
const mockedSystem = vi.mocked(useSystem);
const mockedStatus = vi.mocked(api.system.status);
const mockedRedirect = vi.mocked(redirectToSetup);

const mutateAsync = vi.fn();
const restart = vi.fn();
const onInstalledNeedsRestart = vi.fn();
const onUseInstalled = vi.fn();
const onNext = vi.fn();

function setCatalog(entries: unknown[]) {
  mockedCatalog.mockReturnValue({
    data: { sources: [], entries },
  } as unknown as ReturnType<typeof useProviderHubCatalog>);
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

describe("ProviderInstallStage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setCatalog([opensubtitlesEntry, subsceneEntry]);
    mutateAsync.mockResolvedValue(undefined);
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
});
