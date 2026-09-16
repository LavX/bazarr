import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
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

  it("creates a Plex instance and sets use_plex when the Plex tab is filled", async () => {
    // A destination is a row, not a settings key: the one-time scalar import
    // has already run by the time the wizard is answered, so a wizard that
    // wrote scalars would configure nothing.
    const created: unknown[] = [];
    server.use(
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = await request.json();
        created.push(body);
        return HttpResponse.json({
          id: "2af88684-d7d2-4534-82bb-a6d839cc5c10",
          kind: "plex",
          name: "Plex",
          enabled: true,
          url: "http://10.0.0.9:32400",
          verify_ssl: false,
          api_key_set: true,
          path_mappings: [],
          refresh_movies: true,
          refresh_episodes: true,
          options: {},
        });
      }),
    );
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    // Plex tab is shown first; fill the minimal connection.
    await user.type(screen.getByLabelText(/address/i), "10.0.0.9");
    await user.type(screen.getByLabelText(/token/i), "plex-token");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toEqual({
      kind: "plex",
      name: "Plex",
      enabled: true,
      url: "http://10.0.0.9:32400",
      verify_ssl: false,
      api_key: "plex-token",
    });
    // Both halves: the row refreshes subtitles, and the account scalars are
    // what the recently-added dates, the webhook helper and Autopulse read.
    expect(mutate).toHaveBeenCalledWith(
      expect.objectContaining({
        "settings-general-use_plex": true,
        "settings-plex-ip": "10.0.0.9",
        "settings-plex-port": 32400,
        "settings-plex-ssl": false,
        "settings-plex-apikey": "plex-token",
        "settings-plex-auth_method": "apikey",
      }),
    );
    expect(onNext).toHaveBeenCalled();
  });

  it("names a refused connection instead of advancing as if it saved", async () => {
    server.use(
      http.post("/api/system/media-server-instances", () =>
        HttpResponse.json({ error_code: "invalid_url" }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.type(screen.getByLabelText(/address/i), "10.0.0.9");
    await user.type(screen.getByLabelText(/token/i), "plex-token");
    await user.click(screen.getByRole("button", { name: /continue/i }));

    expect(await screen.findByText(/could not be saved/)).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
    // Setup is never a dead end: the connection can be finished later.
    await user.click(screen.getByRole("button", { name: /continue anyway/i }));
    expect(onNext).toHaveBeenCalled();
  });
});
