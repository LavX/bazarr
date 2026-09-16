import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen } from "@/tests";
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
