import { ReactElement } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSettingsMutation } from "@/apis/hooks";
import type { MediaServerKind } from "@/apis/raw/mediaServers";
import { OnboardingSelectionProvider } from "@/pages/Setup/useOnboardingSelection";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import MediaServerStep from "./MediaServerStep";

// Keep the real barrel (AllProviders' ThemeLoader reads useSystemSettings from
// it) and override only the settings mutation we assert on. The instance rows
// themselves go through the real hooks and MSW.
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

const row = (kind: MediaServerKind, name: string, id: string) => ({
  id,
  kind,
  name,
  enabled: true,
  url: "http://10.0.0.9:8096",
  verify_ssl: true,
  api_key_set: true,
  path_mappings: [],
  refresh_movies: true,
  refresh_episodes: true,
  options: {},
});

function setInstances(rows: Partial<Record<MediaServerKind, unknown[]>>) {
  server.use(
    http.get("/api/system/media-server-instances", ({ request }) => {
      const kind = new URL(request.url).searchParams.get(
        "kind",
      ) as MediaServerKind;
      return HttpResponse.json({ data: rows[kind] ?? [] });
    }),
  );
}

function withSelection(ui: ReactElement) {
  return customRender(
    <OnboardingSelectionProvider>{ui}</OnboardingSelectionProvider>,
  );
}

describe("MediaServerStep", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    setInstances({});
    mockedSettingsMutation.mockReturnValue({
      mutate,
    } as unknown as ReturnType<typeof useSettingsMutation>);
  });

  it("renders no skip control of its own", () => {
    // The wizard shell owns the one skip control; see steps/index.test.tsx.
    withSelection(<MediaServerStep onNext={onNext} />);

    expect(
      screen.queryByRole("button", { name: /skip/i }),
    ).not.toBeInTheDocument();
  });

  it("offers every kind the Connections page does", () => {
    withSelection(<MediaServerStep onNext={onNext} />);

    for (const label of ["Plex", "Jellyfin", "Emby", "Silo"]) {
      expect(
        screen.getByRole("checkbox", { name: new RegExp(`^${label}$`) }),
      ).toBeInTheDocument();
    }
  });

  it("Continue with nothing ticked writes no settings", async () => {
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(
      screen.getByRole("button", { name: /continue without a server/i }),
    );

    expect(mutate).not.toHaveBeenCalled();
    expect(onNext).toHaveBeenCalled();
  });

  it("the whole card is the hit area, not a label inside it", async () => {
    // The Radio it replaced put a 20px label inside a 73px card, so most of
    // the card did nothing when clicked.
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(
      screen.getByText("Connect with the server URL and an API key."),
    );

    expect(screen.getByRole("checkbox", { name: /^Jellyfin$/ })).toBeChecked();
  });

  it("takes several kinds at once", async () => {
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("checkbox", { name: /^Jellyfin$/ }));
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));

    expect(screen.getByRole("checkbox", { name: /^Jellyfin$/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /^Emby$/ })).toBeChecked();
    expect(
      screen.getByRole("button", { name: /set up 2 servers/i }),
    ).toBeInTheDocument();
  });

  it("takes two servers of one kind and names them apart", async () => {
    // media_server_instances has no uniqueness on name or kind, and Settings
    // already lists several per kind. Two rows both called "Emby" would be
    // indistinguishable everywhere the reader meets them later.
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    await user.click(screen.getByRole("button", { name: /add another emby/i }));

    expect(
      screen.getByRole("button", { name: "Remove Emby" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Remove Emby 2" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /set up 2 servers/i }),
    ).toBeInTheDocument();
  });

  it("unticking a kind drops the servers it added", async () => {
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));
    expect(
      screen.getByRole("button", { name: "Remove Silo" }),
    ).toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));

    expect(screen.queryByRole("button", { name: "Remove Silo" })).toBeNull();
    expect(
      screen.getByRole("button", { name: /continue without a server/i }),
    ).toBeInTheDocument();
  });

  it("a saved server reads as connected and cannot be unticked", async () => {
    // A tick means "not connected yet". Once a row is written, unticking it
    // would not undo the write, so the only honest undo is a delete.
    setInstances({ emby: [row("emby", "Living room", "row-1")] });
    withSelection(<MediaServerStep onNext={onNext} />);

    const checkbox = await screen.findByRole("checkbox", { name: /^Emby$/ });
    await waitFor(() => expect(checkbox).toBeChecked());
    expect(checkbox).toBeDisabled();
    expect(await screen.findByText("Living room")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^disconnect$/i }),
    ).toBeInTheDocument();
  });

  it("Disconnect asks first, then deletes the row and clears the switch", async () => {
    const deleted: string[] = [];
    setInstances({ emby: [row("emby", "Living room", "row-1")] });
    server.use(
      http.delete("/api/system/media-server-instances/row-1", () => {
        deleted.push("row-1");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: /^disconnect$/i }),
    );
    expect(screen.getByText(/disconnect living room\?/i)).toBeInTheDocument();
    expect(deleted).toHaveLength(0);

    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[0],
    );

    await waitFor(() => expect(deleted).toEqual(["row-1"]));
    expect(mutate).toHaveBeenCalledWith({
      "settings-general-use_emby": false,
    });
  });
});
