import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useProviderHubProviders, useSettingsMutation } from "@/apis/hooks";
import { customRender, screen } from "@/tests";
import ProviderConfigureStage from "./ProviderConfigureStage";

vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useProviderHubProviders: vi.fn(),
    useSettingsMutation: vi.fn(),
  };
});

const mockedProviders = vi.mocked(useProviderHubProviders);
const mockedSettingsMutation = vi.mocked(useSettingsMutation);

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

describe("ProviderConfigureStage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setProviders([opensubtitles]);
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
});
