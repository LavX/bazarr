import { StrictMode } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useProviderHubProviders,
  useSettingsMutation,
  useSystem,
} from "@/apis/hooks";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import OnboardingWizardView from "./OnboardingWizard";

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubCatalog: vi.fn(),
    useProviderHubInstall: vi.fn(),
    useProviderHubProviders: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystem: vi.fn(),
  };
});

vi.mock("./steps/providers/redirect", () => ({
  redirectToSetup: vi.fn(),
}));

const mutateAsync = vi.fn();

const catalogEntry = {
  provider_id: "opensubtitles",
  name: "OpenSubtitles",
  version: "1.0.0",
  trusted: true,
  manifest: { id: "opensubtitles", name: "OpenSubtitles" },
};

function openProvidersStep(entries: string[] = ["/setup/providers"]) {
  const router = createMemoryRouter(
    [
      { path: "/setup", element: <OnboardingWizardView /> },
      { path: "/setup/:stepKey", element: <OnboardingWizardView /> },
      { path: "/", element: <div>home</div> },
    ],
    { initialEntries: entries, initialIndex: entries.length - 1 },
  );
  rawRender(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
  return router;
}

describe("the providers step is not a dead end", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.clearAllMocks();
    localStorage.setItem("bazarr.onboarding.intent", "discover");
    localStorage.setItem("bazarr.onboarding.step", "providers");

    vi.mocked(useProviderHubProviders).mockReturnValue({
      data: [],
      isLoading: false,
    } as unknown as ReturnType<typeof useProviderHubProviders>);
    vi.mocked(useProviderHubCatalog).mockReturnValue({
      data: { sources: [], entries: [catalogEntry] },
      isPending: false,
      isError: false,
      error: null,
      isFetching: false,
      refetch: vi.fn(),
    } as unknown as ReturnType<typeof useProviderHubCatalog>);
    vi.mocked(useProviderHubInstall).mockReturnValue({
      mutateAsync,
    } as unknown as ReturnType<typeof useProviderHubInstall>);
    vi.mocked(useSettingsMutation).mockReturnValue({
      mutate: vi.fn(),
      mutateAsync: vi.fn(),
    } as unknown as ReturnType<typeof useSettingsMutation>);
    vi.mocked(useSystem).mockReturnValue({
      restart: vi.fn(),
    } as unknown as ReturnType<typeof useSystem>);
  });

  afterEach(() => {
    localStorage.clear();
  });

  it("offers a way past it when nothing is ticked", async () => {
    openProvidersStep();

    expect(
      await screen.findByRole("button", {
        name: /i will pick providers later/i,
      }),
    ).toBeInTheDocument();
    // The consequence, where the reader is, rather than a disabled button.
    expect(
      screen.getByText(/bazarr\+ has nothing to search/i),
    ).toBeInTheDocument();
  });

  it("takes the skip away while an install is running", async () => {
    // Skipping mid-run leaves the installs finishing into a component nobody
    // renders, restarts Bazarr+ under a reader who has moved on, and arms the
    // health poll after its own cleanup has gone.
    let settle: (() => void) | undefined;
    mutateAsync.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const user = userEvent.setup();
    openProvidersStep();

    await user.click(
      await screen.findByRole("checkbox", { name: /opensubtitles/i }),
    );
    await user.click(
      screen.getByRole("button", { name: /install .{0,3} restart/i }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /i will pick providers later/i }),
      ).not.toBeInTheDocument(),
    );

    settle?.();
  });

  it("does not offer to leave setup while the install runs", async () => {
    // Leaving ends the wizard, which unmounts the step: the run carries on
    // into nothing and restarts Bazarr+ at a reader who is already elsewhere.
    let settle: (() => void) | undefined;
    mutateAsync.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const user = userEvent.setup();
    openProvidersStep();

    expect(
      await screen.findByRole("button", { name: /set up later/i }),
    ).not.toBeDisabled();

    await user.click(
      await screen.findByRole("checkbox", { name: /opensubtitles/i }),
    );
    await user.click(
      screen.getByRole("button", { name: /install .{0,3} restart/i }),
    );

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /set up later/i }),
      ).toBeDisabled(),
    );

    settle?.();
  });

  it("does not offer the skip while the configure stage is saving", async () => {
    // On a rerun the step opens on the configure stage, and its Continue
    // writes the settings. Skipping while that write is in the air advances
    // the wizard, and the write's own success then advances it again.
    vi.mocked(useProviderHubProviders).mockReturnValue({
      data: [
        {
          provider_id: "opensubtitles",
          active_version: "1.0.0",
          manifest: { id: "opensubtitles", name: "OpenSubtitles" },
        },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof useProviderHubProviders>);
    vi.mocked(useSettingsMutation).mockReturnValue({
      mutate: vi.fn(),
      mutateAsync: vi.fn(),
      isPending: true,
    } as unknown as ReturnType<typeof useSettingsMutation>);

    openProvidersStep();

    expect(
      await screen.findByRole("heading", { name: /enable and configure/i }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /i will pick providers later/i }),
    ).not.toBeInTheDocument();
  });

  it("lets a busy step finish by advancing the wizard itself", async () => {
    // The guard is for the reader leaving through the browser, not for the
    // step's own work: a provider save ends by advancing the wizard, and
    // blocking that left the next step rendered under the old address.
    vi.mocked(useProviderHubProviders).mockReturnValue({
      data: [
        {
          provider_id: "opensubtitles",
          active_version: "1.0.0",
          manifest: { id: "opensubtitles", name: "OpenSubtitles" },
        },
      ],
      isLoading: false,
    } as unknown as ReturnType<typeof useProviderHubProviders>);
    // Pending from the press until the answer, which is the window the guard
    // reads and the window the step advances in.
    let pending = false;
    vi.mocked(useSettingsMutation).mockImplementation(
      () =>
        ({
          mutate: (_payload: unknown, opts?: { onSuccess?: () => void }) => {
            pending = true;
            setTimeout(() => opts?.onSuccess?.(), 20);
          },
          mutateAsync: vi.fn(),
          isPending: pending,
        }) as unknown as ReturnType<typeof useSettingsMutation>,
    );

    const user = userEvent.setup();
    const router = openProvidersStep();

    await user.click(
      await screen.findByRole("checkbox", { name: /opensubtitles/i }),
    );
    await user.click(screen.getByRole("button", { name: /^continue$/i }));

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/translator"),
    );
  });

  it("takes Back away while the install runs", async () => {
    // Back moves the cursor itself, which unmounts the step before any
    // history guard can see it.
    let settle: (() => void) | undefined;
    mutateAsync.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const user = userEvent.setup();
    openProvidersStep(["/setup/languages", "/setup/providers"]);

    await user.click(
      await screen.findByRole("checkbox", { name: /opensubtitles/i }),
    );
    await user.click(
      screen.getByRole("button", { name: /install .{0,3} restart/i }),
    );

    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /^back$/i }),
      ).not.toBeInTheDocument(),
    );

    settle?.();
  });

  it("browser Back does not leave the install running into nothing", async () => {
    // The skip is hidden while a run is going, but the address bar is another
    // way out of the same screen: going back unmounted the stage while the
    // installs were still finishing, so the restart arrived at nobody and the
    // poll that brings the reader back was armed after its cleanup had run.
    let settle: (() => void) | undefined;
    mutateAsync.mockImplementation(
      () =>
        new Promise<void>((resolve) => {
          settle = resolve;
        }),
    );
    const user = userEvent.setup();
    const router = openProvidersStep(["/", "/setup/providers"]);

    await user.click(
      await screen.findByRole("checkbox", { name: /opensubtitles/i }),
    );
    await user.click(
      screen.getByRole("button", { name: /install .{0,3} restart/i }),
    );
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: /i will pick providers later/i }),
      ).not.toBeInTheDocument(),
    );

    await router.navigate(-1);

    await waitFor(() =>
      expect(router.state.location.pathname).toBe("/setup/providers"),
    );
    expect(
      screen.getByRole("heading", { name: /add subtitle providers/i }),
    ).toBeInTheDocument();

    settle?.();
  });
});
