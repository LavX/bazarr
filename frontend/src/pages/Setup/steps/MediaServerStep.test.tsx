import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import MediaServerStep from "./MediaServerStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the settings mutation we assert on. The instance rows
// themselves go through the real hooks and MSW, so a refused save is a refused
// save and not a mock's opinion.
vi.mock("@/apis/hooks", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useSettingsMutation: vi.fn(),
  };
});

// The Plex account panel is the Connections panel, and it polls Plex. Stub the
// hooks rather than the network so an unauthenticated render is what the
// account flow would actually show first.
vi.mock("@/apis/hooks/plex", () => ({
  usePlexAuthValidationQuery: vi.fn(() => ({
    data: { valid: false, auth_method: "apikey" },
    isLoading: false,
    error: null,
    refetch: vi.fn(),
  })),
  usePlexPinMutation: vi.fn(() => ({ mutateAsync: vi.fn() })),
  usePlexPinCheckQuery: vi.fn(() => ({ data: undefined })),
  usePlexLogoutMutation: vi.fn(() => ({ mutate: vi.fn(), isPending: false })),
  usePlexServersQuery: vi.fn(() => ({
    data: [],
    error: null,
    refetch: vi.fn(),
  })),
  usePlexSelectedServerQuery: vi.fn(() => ({ data: undefined })),
  usePlexServerSelectionMutation: vi.fn(() => ({ mutateAsync: vi.fn() })),
}));

const mockedSettingsMutation = vi.mocked(useSettingsMutation);

const onNext = vi.fn();
const mutate = vi.fn();

// The shape the API answers a create with, so the real hook parses it.
const createdInstance = (kind: string, url: string) => ({
  id: "2af88684-d7d2-4534-82bb-a6d839cc5c10",
  kind,
  name: "Emby",
  enabled: true,
  url,
  verify_ssl: true,
  api_key_set: true,
  path_mappings: [],
  refresh_movies: true,
  refresh_episodes: true,
  options: {},
});

describe("MediaServerStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("renders no skip control of its own", () => {
    // The wizard shell owns the one skip control; see steps/index.test.tsx.
    customRender(<MediaServerStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });

  it("offers every kind the Connections page does", () => {
    customRender(<MediaServerStep onNext={onNext} />);

    for (const label of ["Plex", "Jellyfin", "Emby", "Silo"]) {
      expect(
        screen.getByRole("radio", { name: new RegExp(label) }),
      ).toBeInTheDocument();
    }
  });

  it("Continue with nothing filled in writes no settings", async () => {
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /continue without a server/i }),
    );

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("configures one kind at a time", async () => {
    // Tabs put two servers' fields on screen at once, on a step most people
    // skip. Picking a kind is what puts its fields there, and only its fields.
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    expect(screen.queryByLabelText(/server url/i)).not.toBeInTheDocument();

    await user.click(screen.getByRole("radio", { name: /^Jellyfin/ }));
    expect(screen.getByLabelText(/server url/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/api key/i)).toBeInTheDocument();
    // Jellyfin resolves the item itself, so it never had path mappings.
    expect(screen.queryByText(/^Path mappings$/)).not.toBeInTheDocument();
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );

    await user.click(screen.getByRole("radio", { name: /^Emby/ }));
    expect(screen.getByText(/^Path mappings$/)).toBeInTheDocument();
    expect(screen.queryByLabelText(/movie libraries/i)).not.toBeInTheDocument();
    // Nothing carries over from the kind just left, the prefilled name least
    // of all: a reused instance would label the Emby row "Jellyfin".
    expect(screen.getByLabelText(/^name$/i)).toHaveValue("Emby");
    expect(screen.getByLabelText(/server url/i)).toHaveValue("");
  });

  it("Plex connects through the account flow and writes nothing itself", async () => {
    // The manual host, port and pasted X-Plex-Token this step used to ask for
    // is a path the rest of the app has moved off: the OAuth callback stores
    // the token, sets use_plex, and media_servers.plex_account owns the row.
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Plex/ }));

    expect(
      screen.getByRole("button", { name: /connect to plex/i }),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText(/token/i)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/port/i)).not.toBeInTheDocument();
    expect(mutate).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /^continue$/i }));
    expect(onNext).toHaveBeenCalled();
    expect(mutate).not.toHaveBeenCalled();
  });

  it("Emby asks for the path mappings its refreshes are scoped by", async () => {
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Emby/ }));
    await user.click(screen.getByRole("button", { name: /add mapping/i }));

    expect(screen.getByLabelText(/local path 1/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/server path 1/i)).toBeInTheDocument();
  });

  it("creates the instance and flips the master switch", async () => {
    const created: unknown[] = [];
    server.use(
      http.post("/api/system/media-server-instances", async ({ request }) => {
        const body = await request.json();
        created.push(body);
        return HttpResponse.json(
          createdInstance("emby", "http://10.0.0.9:8096"),
        );
      }),
    );
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Emby/ }));
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "emby-key");
    await user.click(screen.getByRole("button", { name: /add mapping/i }));
    await user.type(screen.getByLabelText(/local path 1/i), "/tv");
    await user.type(screen.getByLabelText(/server path 1/i), "/media/tv");
    await user.click(screen.getByRole("button", { name: /connect emby/i }));

    await waitFor(() => expect(created).toHaveLength(1));
    expect(created[0]).toEqual({
      kind: "emby",
      name: "Emby",
      enabled: true,
      url: "http://10.0.0.9:8096",
      verify_ssl: true,
      api_key: "emby-key",
      path_mappings: [{ local_path: "/tv", remote_path: "/media/tv" }],
      options: {},
    });
    expect(mutate).toHaveBeenCalledWith(
      { "settings-general-use_emby": true },
      expect.anything(),
    );
  });

  it("refuses to save an enabled Emby instance with no mapping", async () => {
    // The Connections form has always refused this. A wizard that allowed it
    // would hand Settings a row its own editor calls invalid.
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Emby/ }));
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "emby-key");
    await user.click(screen.getByRole("button", { name: /connect emby/i }));

    expect(
      await screen.findByText(/needs at least one path mapping/i),
    ).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
  });

  it("names a refused connection instead of advancing as if it saved", async () => {
    server.use(
      http.post("/api/system/media-server-instances", () =>
        HttpResponse.json({ error_code: "invalid_url" }, { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Emby/ }));
    await user.type(
      screen.getByLabelText(/server url/i),
      "http://10.0.0.9:8096",
    );
    await user.type(screen.getByLabelText(/api key/i), "emby-key");
    await user.click(screen.getByRole("button", { name: /add mapping/i }));
    await user.type(screen.getByLabelText(/local path 1/i), "/tv");
    await user.type(screen.getByLabelText(/server path 1/i), "/media/tv");
    await user.click(screen.getByRole("button", { name: /connect emby/i }));

    expect(
      await screen.findByText(/could not save this instance/i),
    ).toBeInTheDocument();
    expect(onNext).not.toHaveBeenCalled();
    // Setup is never a dead end: the shell's skip moves on, and the alert says
    // where the connection can be finished.
    expect(
      within(screen.getByRole("alert")).getByText(/settings, connections/i),
    ).toBeInTheDocument();
  });

  it("Silo scopes every mapping to a library", async () => {
    const user = userEvent.setup();
    customRender(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("radio", { name: /^Silo/ }));

    expect(
      screen.getByRole("button", { name: /load libraries/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        /select the silo library that contains that server folder/i,
      ),
    ).toBeInTheDocument();
  });
});
