import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  useLanguageProfiles,
  useLanguages,
  useSettingsMutation,
} from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import LanguagesStep from "./LanguagesStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the hooks this step drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useLanguages: vi.fn(),
    useLanguageProfiles: vi.fn(),
    useSettingsMutation: vi.fn(),
  };
});

const mockedUseLanguages = vi.mocked(useLanguages);
const mockedUseLanguageProfiles = vi.mocked(useLanguageProfiles);
const mockedUseSettingsMutation = vi.mocked(useSettingsMutation);

const onNext = vi.fn();
const mutate = vi.fn();

const LANGUAGES: Language.Server[] = [
  { code2: "en", code3: "eng", name: "English", enabled: false },
  { code2: "es", code3: "spa", name: "Spanish", enabled: false },
];

function setLanguages(data: unknown) {
  mockedUseLanguages.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useLanguages>);
}

function setProfiles(data: unknown) {
  mockedUseLanguageProfiles.mockReturnValue({
    data,
  } as unknown as ReturnType<typeof useLanguageProfiles>);
}

describe("LanguagesStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setLanguages(LANGUAGES);
    setProfiles([]);
    mockedUseSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("disables Continue until a language is selected", async () => {
    const user = userEvent.setup();
    customRender(<LanguagesStep onNext={onNext} />);

    const button = screen.getByRole("button", { name: /continue/i });
    expect(button).toBeDisabled();

    await user.click(screen.getByRole("combobox", { name: "Languages" }));
    await user.click(await screen.findByText("English"));

    expect(
      screen.getByRole("button", { name: /continue/i }),
    ).not.toBeDisabled();
  });

  it("persists a default profile built from the selected languages", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<LanguagesStep onNext={onNext} />);

    await user.click(screen.getByRole("combobox", { name: "Languages" }));
    await user.click(await screen.findByText("English"));
    await user.click(await screen.findByText("Spanish"));

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledTimes(1);
    const [payload] = mutate.mock.calls[0];

    expect(payload["languages-enabled"]).toEqual(["en", "es"]);
    expect(payload["settings-general-serie_default_enabled"]).toBe(true);
    expect(payload["settings-general-serie_default_profile"]).toBe(1);
    expect(payload["settings-general-movie_default_enabled"]).toBe(true);
    expect(payload["settings-general-movie_default_profile"]).toBe(1);

    const profiles = JSON.parse(payload["languages-profiles"]);
    expect(profiles).toHaveLength(1);
    const profile = profiles[0];
    expect(profile.profileId).toBe(1);
    expect(profile.items).toHaveLength(2);
    expect(
      profile.items.map((it: Language.ProfileItem) => it.language),
    ).toEqual(["en", "es"]);
    expect(profile.items[0]).toMatchObject({
      id: 1,
      language: "en",
      hi: "False",
      forced: "False",
      audio_exclude: "False",
      audio_only_include: "False",
      translate_from: null,
    });
    expect(profile.items[1].id).toBe(2);

    // onNext fires only after the mutation succeeds.
    expect(onNext).not.toHaveBeenCalled();
    onSuccess?.();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
  });

  it("proceeds without rewriting when a profile already exists", async () => {
    const user = userEvent.setup();
    setProfiles([
      {
        name: "Existing",
        profileId: 1,
        cutoff: null,
        items: [],
        mustContain: [],
        mustNotContain: [],
        originalFormat: false,
        tag: undefined,
      },
    ]);

    customRender(<LanguagesStep onNext={onNext} />);

    expect(screen.getByText(/already configured/i)).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("renders a Back button when onBack is provided", () => {
    const onBack = vi.fn();
    customRender(<LanguagesStep onNext={onNext} onBack={onBack} />);

    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
  });
});
