import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useProviderHubProviders,
  useSettingsMutation,
  useSystem,
} from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import OnboardingWizardView from "./OnboardingWizard";

vi.mock("react-router", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-router")>();
  return {
    ...actual,
    useNavigate: () => vi.fn(),
  };
});

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
    customRender(<OnboardingWizardView />);

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
    customRender(<OnboardingWizardView />);

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
});
