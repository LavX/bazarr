import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { http, HttpResponse } from "msw";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import TranslatorStep from "./TranslatorStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the settings mutation we assert on.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

const onNext = vi.fn();
const mutate = vi.fn();

describe("TranslatorStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("saves the key and selects OpenRouter as the engine", async () => {
    // The key on its own would be inert: translator_type still defaults to
    // Google Translate, so the user would leave setup with a key that
    // translates nothing.
    const user = userEvent.setup();
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        opts?.onSuccess?.();
      },
    );

    customRender(<TranslatorStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/openrouter api key/i), "sk-or-xyz");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledWith(
      {
        "settings-translator-openrouter_api_key": "sk-or-xyz",
        "settings-translator-translator_type": "openrouter",
      },
      expect.anything(),
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("says so when the key cannot be saved", async () => {
    // A restarting or unreachable Bazarr+ is kept out of the toasts, so the
    // step is the only place a failed write can be reported.
    const user = userEvent.setup();
    mutate.mockImplementationOnce(
      (_input: unknown, opts?: { onError?: () => void }) => {
        opts?.onError?.();
      },
    );

    customRender(<TranslatorStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/openrouter api key/i), "sk-or-xyz");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /could not save the key/i,
    );
    expect(onNext).not.toHaveBeenCalled();
  });

  it("says OpenRouter is paid and that a key replaces Google Translate", () => {
    // The step invited a newcomer to switch engines without a word about
    // cost, then wrote translator_type over the default without saying so.
    customRender(<TranslatorStep onNext={onNext} />);

    expect(screen.getByText(/openrouter is a paid api/i)).toBeInTheDocument();
    expect(
      screen.getByLabelText(/openrouter api key/i),
    ).toHaveAccessibleDescription(
      "Saving a key switches the translation engine from Google Translate to OpenRouter. Leave this empty to keep Google Translate.",
    );
  });

  it("reports a failed save once, inline, with no toast", async () => {
    // Through the real settings hook, not a mock of it: a 503 is the failure
    // a restart mid-setup produces, and the hook's own "Save failed" toast
    // would announce it a second time beside the inline alert.
    const { useSettingsMutation: realSettingsMutation } =
      await vi.importActual<typeof import("@/apis/hooks")>("@/apis/hooks");
    mockedSettingsMutation.mockImplementation(realSettingsMutation);
    server.use(
      http.post(
        "/api/system/settings",
        () => new HttpResponse(null, { status: 503 }),
      ),
    );
    const user = userEvent.setup();
    customRender(<TranslatorStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/openrouter api key/i), "sk-or-xyz");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(1));
    expect(screen.queryByText("Save failed")).not.toBeInTheDocument();
    expect(mockedSettingsMutation).toHaveBeenCalledWith(false, {
      inlineErrors: true,
    });
    expect(onNext).not.toHaveBeenCalled();
  });

  it("Continue with no key writes nothing", async () => {
    const user = userEvent.setup();
    customRender(<TranslatorStep onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /continue without translation/i }),
    );

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("renders no skip control of its own", () => {
    customRender(<TranslatorStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });
});
