import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation, useSystemSettings } from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import GeneralStep from "./GeneralStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the hooks this step drives.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSystemSettings: vi.fn(),
    useSettingsMutation: vi.fn(),
  };
});

const mockedUseSystemSettings = vi.mocked(useSystemSettings);
const mockedUseSettingsMutation = vi.mocked(useSettingsMutation);

const onNext = vi.fn();
const mutate = vi.fn();

function setGeneral(general: Partial<Settings.General>) {
  mockedUseSystemSettings.mockReturnValue({
    data: { general },
  } as unknown as ReturnType<typeof useSystemSettings>);
}

describe("GeneralStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    setGeneral({
      subfolder: "current",
      subfolder_custom: "",
      upgrade_subs: true,
      page_size: 50,
    });
    mockedUseSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("renders the three controls pre-filled from settings", () => {
    customRender(<GeneralStep onNext={onNext} />);

    // Subtitle folder selector reflects the stored value.
    expect(
      screen.getByRole("combobox", { name: /subtitle folder/i }),
    ).toHaveValue("AlongSide Media File");
    // Page size selector reflects the stored value.
    expect(screen.getByRole("combobox", { name: /page size/i })).toHaveValue(
      "50",
    );
    // Upgrade switch is on.
    expect(
      screen.getByRole("switch", { name: /upgrade previously downloaded/i }),
    ).toBeChecked();
  });

  it("writes a changed page size on Continue", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<GeneralStep onNext={onNext} />);

    await user.click(screen.getByRole("combobox", { name: /page size/i }));
    await user.click(await screen.findByText("100"));

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledTimes(1);
    const [payload] = mutate.mock.calls[0];
    expect(payload["settings-general-page_size"]).toBe(100);

    expect(onNext).not.toHaveBeenCalled();
    onSuccess?.();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
  });

  it("reveals and writes a custom subfolder when a non-default folder is picked", async () => {
    const user = userEvent.setup();
    let onSuccess: (() => void) | undefined;
    mutate.mockImplementation(
      (_input: unknown, opts?: { onSuccess?: () => void }) => {
        onSuccess = opts?.onSuccess;
      },
    );

    customRender(<GeneralStep onNext={onNext} />);

    // Pick "Absolute Path" which is not the alongside-media default.
    await user.click(
      screen.getByRole("combobox", { name: /subtitle folder/i }),
    );
    await user.click(await screen.findByText("Absolute Path"));

    // The custom-folder input is now revealed; fill it.
    const custom = await screen.findByRole("textbox", {
      name: /custom subtitles folder/i,
    });
    await user.type(custom, "/mnt/subs");

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledTimes(1);
    const [payload] = mutate.mock.calls[0];
    expect(payload["settings-general-subfolder"]).toBe("absolute");
    expect(payload["settings-general-subfolder_custom"]).toBe("/mnt/subs");

    onSuccess?.();
    await waitFor(() => expect(onNext).toHaveBeenCalled());
  });

  it("advances without a mutation when nothing changed", async () => {
    const user = userEvent.setup();
    customRender(<GeneralStep onNext={onNext} />);

    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("renders a Back button when onBack is provided", () => {
    const onBack = vi.fn();
    customRender(<GeneralStep onNext={onNext} onBack={onBack} />);

    expect(screen.getByRole("button", { name: /back/i })).toBeInTheDocument();
  });
});
