import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useProviderHubProviders,
  useSettingsMutation,
  useSystemSettings,
} from "@/apis/hooks";
import { customRender, screen } from "@/tests";
import ProviderConfigureStage from "./ProviderConfigureStage";

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubProviders: vi.fn(),
    useSettingsMutation: vi.fn(),
    useSystemSettings: vi.fn(),
  };
});

const mockedProviders = vi.mocked(useProviderHubProviders);
const mockedSettingsMutation = vi.mocked(useSettingsMutation);
const mockedSystemSettings = vi.mocked(useSystemSettings);

const onNext = vi.fn();
const onInstallMore = vi.fn();
const settingsMutate = vi.fn();

const opensubtitles = {
  provider_id: "opensubtitles",
  name: "OpenSubtitles",
  state: "active",
  manifest: {
    id: "opensubtitles",
    name: "OpenSubtitles",
    config_schema: {
      required: ["username", "password"],
      properties: {
        username: { type: "string", title: "Username" },
        password: { type: "string", title: "Password", secret: true },
        // Advanced, non-required option: first-run must hide it.
        only_forced: {
          type: "boolean",
          title: "Only forced subtitles",
        },
      },
    },
  },
};

function setProviders(data: unknown[]) {
  mockedProviders.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useProviderHubProviders>);
}

function setEnabledProviders(ids: string[]) {
  mockedSystemSettings.mockReturnValue({
    data: { general: { enabled_providers: ids } },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

describe("ProviderConfigureStage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setProviders([opensubtitles]);
    setEnabledProviders([]);
    mockedSettingsMutation.mockReturnValue({
      mutate: settingsMutate,
      isPending: false,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("disables Continue until at least one provider is enabled", () => {
    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(screen.getByRole("button", { name: /continue/i })).toBeDisabled();
  });

  it("saves enabled_providers and per-provider credentials, then advances", async () => {
    const user = userEvent.setup();
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    // Enabling reveals the required credential fields.
    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    // Advanced, non-required options stay hidden on first run.
    expect(screen.queryByText("Only forced subtitles")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "s3cret");

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledWith(
      {
        "settings-general-enabled_providers": ["opensubtitles"],
        "settings-opensubtitles-username": "alice",
        "settings-opensubtitles-password": "s3cret",
      },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("calls onInstallMore from the Install more providers link", async () => {
    const user = userEvent.setup();
    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(
      screen.getByRole("button", { name: /install more providers/i }),
    );

    expect(onInstallMore).toHaveBeenCalled();
  });

  // "Install recommended" enables its providers as it installs them, and this
  // screen writes the whole enabled list, so it has to start from what is
  // actually enabled. Starting empty asked the reader the same question twice,
  // and ticking one provider by hand then turned the rest of the set off.
  it("starts from the providers Bazarr+ already has enabled", async () => {
    const user = userEvent.setup();
    setProviders([
      opensubtitles,
      {
        provider_id: "subtitlecat",
        name: "SubtitleCat",
        state: "active",
        manifest: { id: "subtitlecat", name: "SubtitleCat" },
      },
      {
        provider_id: "subdl",
        name: "SubDL",
        state: "active",
        manifest: { id: "subdl", name: "SubDL" },
      },
    ]);
    setEnabledProviders(["opensubtitles", "subtitlecat"]);
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(
      screen.getByRole("checkbox", { name: /opensubtitles/i }),
    ).toBeChecked();
    expect(
      screen.getByRole("checkbox", { name: /subtitlecat/i }),
    ).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /subdl/i })).not.toBeChecked();

    // Continue is answerable straight away, and the saved list keeps the set.
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledWith(
      expect.objectContaining({
        "settings-general-enabled_providers": ["opensubtitles", "subtitlecat"],
      }),
      expect.anything(),
    );
  });

  // The enabled list is refreshed by the write that enabled the recommended
  // set, and it arrives after this screen has already rendered once.
  it("follows the enabled list until the reader answers for themselves", async () => {
    const user = userEvent.setup();
    setProviders([
      opensubtitles,
      {
        provider_id: "subtitlecat",
        name: "SubtitleCat",
        state: "active",
        manifest: { id: "subtitlecat", name: "SubtitleCat" },
      },
    ]);
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    const { rerender } = customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );
    expect(
      screen.getByRole("checkbox", { name: /subtitlecat/i }),
    ).not.toBeChecked();

    setEnabledProviders(["subtitlecat"]);
    rerender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(
      screen.getByRole("checkbox", { name: /subtitlecat/i }),
    ).toBeChecked();

    // An answer of their own is not undone by a later refresh.
    await user.click(screen.getByRole("checkbox", { name: /subtitlecat/i }));
    setEnabledProviders(["subtitlecat"]);
    rerender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(
      screen.getByRole("checkbox", { name: /subtitlecat/i }),
    ).not.toBeChecked();
  });
});
