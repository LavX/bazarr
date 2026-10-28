/* eslint-disable camelcase */
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
const refetchProviders = vi.fn();

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
    isError: false,
    error: null,
    isFetching: false,
    refetch: refetchProviders,
  } as unknown as ReturnType<typeof useProviderHubProviders>);
}

function setProvidersError(error: unknown) {
  mockedProviders.mockReturnValue({
    data: undefined,
    isError: true,
    error,
    isFetching: false,
    refetch: refetchProviders,
  } as unknown as ReturnType<typeof useProviderHubProviders>);
}

// `sections` is the rest of the settings payload: a provider's stored
// credentials live under its own id, exactly as the Settings page reads them.
function setSettings(ids: string[], sections: Record<string, unknown> = {}) {
  mockedSystemSettings.mockReturnValue({
    data: { general: { enabled_providers: ids }, ...sections },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

function setEnabledProviders(ids: string[]) {
  setSettings(ids);
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
    expect(settingsMutate.mock.calls[0][0]).not.toHaveProperty(
      "settings-general-disabled_providers",
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("removes enabled providers from the disabled list in the same save", async () => {
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
    setSettings(["opensubtitles"], {
      general: {
        enabled_providers: ["opensubtitles"],
        disabled_providers: [
          "subdl",
          "subtitlecat",
          "opensubtitles",
          "subscene",
        ],
      },
      opensubtitles: { username: "alice", password: "stored" },
    });

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(screen.getByRole("checkbox", { name: /subtitlecat/i }));
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledTimes(1);
    expect(settingsMutate).toHaveBeenCalledWith(
      {
        "settings-general-enabled_providers": ["opensubtitles", "subtitlecat"],
        "settings-general-disabled_providers": ["subdl", "subscene"],
      },
      expect.anything(),
    );
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
    // Stored credentials are credentials: a provider configured before the
    // wizard ran must not be reported as missing what it already has.
    setSettings(["opensubtitles", "subtitlecat"], {
      opensubtitles: { username: "alice", password: "stored" },
    });
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

  // The stage computed the required fields only to decide what to render, so a
  // provider could be ticked, left blank and saved enabled. The reader then
  // met silent search failures with nothing to connect them to.
  it("refuses to save an enabled provider whose required credentials are blank", async () => {
    const user = userEvent.setup();

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(screen.getByRole("checkbox", { name: /opensubtitles/i }));
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
    expect(
      screen.getByText(/opensubtitles needs username, password/i),
    ).toBeInTheDocument();

    // Filling them in clears the block.
    await user.type(screen.getByLabelText("Username"), "alice");
    await user.type(screen.getByLabelText("Password"), "s3cret");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalled();
  });

  // An optional account name beside a required secret was hidden, so the step
  // asked for half a login and saved an account that cannot sign in.
  it("asks for the account name that goes with a secret", async () => {
    const user = userEvent.setup();
    setProviders([
      {
        provider_id: "halfling",
        name: "Halfling",
        state: "active",
        manifest: {
          id: "halfling",
          name: "Halfling",
          config_schema: {
            required: ["password"],
            properties: {
              username: { type: "string", title: "Username" },
              password: { type: "string", title: "Password", secret: true },
              request_delay_ms: { type: "integer", title: "Request delay" },
            },
          },
        },
      },
    ]);

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(screen.getByRole("checkbox", { name: /halfling/i }));

    expect(screen.getByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    // Advanced options stay hidden.
    expect(screen.queryByText("Request delay")).not.toBeInTheDocument();
  });

  // The raw client swallows 502/503 and network errors, which is what the
  // backend produces while it comes back from the install restart, so a failed
  // save on this step left nothing at all on screen.
  it("surfaces a failed save instead of going quiet", async () => {
    const user = userEvent.setup();
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onError?: (reason: unknown) => void }) => {
        opts?.onError?.(new Error("the backend said no"));
      },
    );
    setSettings(["subtitlecat"]);
    setProviders([
      {
        provider_id: "subtitlecat",
        name: "SubtitleCat",
        state: "active",
        manifest: { id: "subtitlecat", name: "SubtitleCat" },
      },
    ]);

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByText(/the backend said no/i)).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  // A list that failed to load used to render as a list of nothing: a heading,
  // a rule about enabling one provider, an empty region and a dead Continue.
  it("reports a provider list that failed to load, with a retry", async () => {
    const user = userEvent.setup();
    setProvidersError(new Error("provider hub unreachable"));

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(
      screen.getByText(/could not load the installed providers/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/provider hub unreachable/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /retry/i }));
    expect(refetchProviders).toHaveBeenCalled();
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

  // A plugin secret never reads back from the settings API: the section says
  // it is configured through the descriptor instead. The wizard has to take
  // that word for it, or a provider credentialed before the wizard demands
  // its credential again, and saving would strip it.
  it("shows a stored secret as configured and keeps it untouched on save", async () => {
    const user = userEvent.setup();
    setSettings(["opensubtitles"], {
      opensubtitles: {
        username: "alice",
        password: "",
        __secret_fields: {
          password: { configured: true, last_four: "1a2b" },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    // The stored secret reads as an empty box with the descriptor's word for
    // it beside it, not as a missing credential. A stored credential is
    // replaced through an explicit action: the box is read-only until
    // Replace is pressed.
    expect(
      await screen.findByText(/configured, ends in 1a2b/i),
    ).toBeInTheDocument();
    const storedInput = screen.getByLabelText("Password");
    expect(storedInput).toHaveValue("");
    expect(storedInput).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /replace/i }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    // Absent means unchanged: nothing about the secret is submitted.
    expect(settingsMutate).toHaveBeenCalledTimes(1);
    const payload = settingsMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("settings-opensubtitles-password");
    expect(onNext).toHaveBeenCalled();
  });

  // Clear is explicit: the button stages an empty value that the save
  // submits as the clear, distinct from leaving the box untouched which
  // keeps the stored secret.
  it("clearing a stored secret submits an explicit clear", async () => {
    const user = userEvent.setup();
    // The secret is optional here, so clearing it is a valid end state.
    setProviders([
      {
        provider_id: "secretvault",
        name: "SecretVault",
        state: "active",
        manifest: {
          id: "secretvault",
          name: "SecretVault",
          config_schema: {
            properties: {
              password: { type: "string", title: "Password", secret: true },
            },
          },
        },
      },
    ]);
    setSettings(["secretvault"], {
      secretvault: {
        password: "",
        __secret_fields: {
          password: { configured: true, last_four: "c0de" },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    await user.click(screen.getByRole("button", { name: /clear/i }));

    expect(
      screen.getByText(/will be cleared when you continue/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledWith(
      {
        "settings-general-enabled_providers": ["secretvault"],
        "settings-secretvault-password": "",
      },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });

  // Only the Clear button stages the removal. Emptying the input by typing
  // is editing, not clearing: the staged answer goes away, the stored secret
  // stands, and the save submits nothing about it.
  it("emptying a typed value unstages the field instead of clearing the key", async () => {
    const user = userEvent.setup();
    setSettings(["opensubtitles"], {
      opensubtitles: {
        username: "alice",
        password: "",
        __secret_fields: {
          password: { configured: true, last_four: "c0de" },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    const input = screen.getByLabelText("Password");
    // Typing is impossible until Replace is pressed.
    await user.click(screen.getByRole("button", { name: /replace/i }));
    await user.type(input, "a-typed-replacement");
    expect(
      screen.getByText(/replaces the stored key when you continue/i),
    ).toBeInTheDocument();

    // Typing and then emptying the box unstages the field: no clear is
    // submitted, the stored key is kept.
    await user.clear(input);
    expect(screen.getByText(/configured, ends in c0de/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/will be cleared when you continue/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledTimes(1);
    const payload = settingsMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("settings-opensubtitles-password");
    expect(onNext).toHaveBeenCalled();
  });

  // Cancel takes a staged replacement back out and returns the field to
  // Configured: the box locks again, and the save submits nothing about the
  // secret.
  it("cancel returns a staged replacement to Configured", async () => {
    const user = userEvent.setup();
    setSettings(["opensubtitles"], {
      opensubtitles: {
        username: "alice",
        password: "",
        __secret_fields: {
          password: { configured: true, last_four: "c0de" },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    const input = screen.getByLabelText("Password");
    await user.click(screen.getByRole("button", { name: /replace/i }));
    await user.type(input, "unit-replacement");
    expect(
      screen.getByText(/replaces the stored key when you continue/i),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /cancel/i }));

    expect(input).toBeDisabled();
    expect(screen.getByText(/configured, ends in c0de/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/replaces the stored key when you continue/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledTimes(1);
    const payload = settingsMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("settings-opensubtitles-password");
    expect(onNext).toHaveBeenCalled();
  });

  // A replacement made only of spaces is not a credential: the input treats
  // it as blank, the staged answer goes away, and the save submits nothing
  // about the secret. Staging it as a clear would have saved the spaces.
  it("treats a spaces-only replacement as blank instead of saving the spaces", async () => {
    const user = userEvent.setup();
    setSettings(["opensubtitles"], {
      opensubtitles: {
        username: "alice",
        password: "",
        __secret_fields: {
          password: { configured: true, last_four: "c0de" },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    const input = screen.getByLabelText("Password");
    await user.click(screen.getByRole("button", { name: /replace/i }));
    await user.type(input, "   ");

    // Nothing is staged: the box keeps the stored secret's word for it,
    // and neither a replacement nor a clear is said.
    expect(screen.getByText(/configured, ends in c0de/i)).toBeInTheDocument();
    expect(
      screen.queryByText(/will be cleared when you continue/i),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/replaces the stored key when you continue/i),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledTimes(1);
    const payload = settingsMutate.mock.calls[0][0] as Record<string, unknown>;
    expect(payload).not.toHaveProperty("settings-opensubtitles-password");
    expect(onNext).toHaveBeenCalled();
  });

  // A stored credential that failed to decrypt is not usable: the gate
  // demands a replacement before Continue proceeds, and the box says why.
  it("demands a replacement for a credential that failed to decrypt", async () => {
    const user = userEvent.setup();
    setProviders([
      {
        provider_id: "secretvault",
        name: "SecretVault",
        state: "active",
        manifest: {
          id: "secretvault",
          name: "SecretVault",
          config_schema: {
            required: ["password"],
            properties: {
              password: { type: "string", title: "Password", secret: true },
            },
          },
        },
      },
    ]);
    setSettings(["secretvault"], {
      secretvault: {
        password: "",
        __secret_fields: {
          password: { configured: true, decrypt_failed: true },
        },
      },
    });
    settingsMutate.mockImplementation(
      (_body: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(
      <ProviderConfigureStage onNext={onNext} onInstallMore={onInstallMore} />,
    );

    expect(
      await screen.findByText(/saved value could not be decrypted/i),
    ).toBeInTheDocument();
    // Like every stored credential, the failed one is read-only until
    // Replace is pressed.
    expect(screen.getByLabelText("Password")).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    // The configured flag is not enough: the credential is unusable, so the
    // save is refused with the missing-credentials message.
    expect(settingsMutate).not.toHaveBeenCalled();
    expect(onNext).not.toHaveBeenCalled();
    expect(screen.getByText(/secretvault needs password/i)).toBeInTheDocument();

    // A replacement is what it takes: the staged replacement comes first in
    // the text, so the reader is told it replaces the stored key, not asked
    // to re-enter it again.
    await user.click(screen.getByRole("button", { name: /replace/i }));
    await user.type(screen.getByLabelText("Password"), "unit-replacement");
    expect(
      screen.getByText(/replaces the stored key when you continue/i),
    ).toBeInTheDocument();
    expect(
      screen.queryByText(/saved value could not be decrypted/i),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(settingsMutate).toHaveBeenCalledWith(
      {
        "settings-general-enabled_providers": ["secretvault"],
        "settings-secretvault-password": "unit-replacement",
      },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });
});
