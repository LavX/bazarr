import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubCatalog,
  useProviderHubInstall,
  useProviderHubProviders,
  useSettingsMutation,
  useSystem,
} from "@/apis/hooks";
import { customRender, screen } from "@/tests";
import ProvidersStep from "./ProvidersStep";

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubCatalog: vi.fn(),
    useProviderHubProviders: vi.fn(),
    useProviderHubInstall: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystem: vi.fn(),
  };
});

const mockedCatalog = vi.mocked(useProviderHubCatalog);
const mockedProviders = vi.mocked(useProviderHubProviders);
const mockedInstall = vi.mocked(useProviderHubInstall);
const mockedSettingsMutation = vi.mocked(useSettingsMutation);
const mockedSystem = vi.mocked(useSystem);

const onNext = vi.fn();

function setProviders(data: unknown[]) {
  mockedProviders.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useProviderHubProviders>);
}

describe("ProvidersStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedCatalog.mockReturnValue({
      data: {
        sources: [],
        entries: [
          {
            provider_id: "opensubtitles",
            name: "OpenSubtitles",
            version: "1.0.0",
            trusted: true,
            manifest: { id: "opensubtitles", name: "OpenSubtitles" },
          },
        ],
      },
    } as unknown as ReturnType<typeof useProviderHubCatalog>);
    mockedInstall.mockReturnValue({
      mutateAsync: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useProviderHubInstall>);
    mockedSettingsMutation.mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
    mockedSystem.mockReturnValue({
      restart: vi.fn(),
      isMutating: false,
    } as unknown as ReturnType<typeof useSystem>);
  });

  it("renders the install stage when no providers are installed", () => {
    setProviders([]);

    customRender(<ProvidersStep onNext={onNext} />);

    expect(
      screen.getByRole("button", { name: /install & restart/i }),
    ).toBeInTheDocument();
  });

  it("does not latch the install catalog while the installed list is still loading", () => {
    // Post-restart resume: the page reloads and the installed-providers query
    // starts empty/loading. The stage must NOT latch to install here, or the
    // user is wrongly dropped back on the catalog instead of resuming configure.
    mockedProviders.mockReturnValue({
      data: undefined,
      isLoading: true,
    } as unknown as ReturnType<typeof useProviderHubProviders>);

    customRender(<ProvidersStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /install & restart/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("checkbox", { name: /opensubtitles/i }),
    ).not.toBeInTheDocument();
  });

  it("renders the configure stage when a provider is already installed (resume)", () => {
    setProviders([
      {
        provider_id: "opensubtitles",
        name: "OpenSubtitles",
        state: "active",
        manifest: { id: "opensubtitles", name: "OpenSubtitles" },
      },
    ]);

    customRender(<ProvidersStep onNext={onNext} />);

    // The configure stage shows the per-provider enable toggle + Continue.
    expect(
      screen.getByRole("checkbox", { name: /opensubtitles/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /continue/i }),
    ).toBeInTheDocument();
  });
});
