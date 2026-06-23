import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen } from "@/tests";
import MediaServerStep from "./MediaServerStep";

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

describe("MediaServerStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("Skip advances without writing any settings", async () => {
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("button", { name: /skip/i }));

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("writes Plex keys and use_plex when the Plex tab is filled", async () => {
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    // Plex tab is shown first; fill the minimal connection.
    await user.type(screen.getByLabelText(/address/i), "10.0.0.9");
    await user.type(screen.getByLabelText(/token/i), "plex-token");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        "settings-plex-ip": "10.0.0.9",
        "settings-plex-apikey": "plex-token",
        "settings-plex-auth_method": "apikey",
        "settings-general-use_plex": true,
      }),
    );
    expect(onNext).toHaveBeenCalled();
  });
});
