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

/** Which Plex row the account owns, as the backend records it. */
function setPlexOwner(instanceId: string) {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {},
        translator: {},
        // eslint-disable-next-line camelcase
        plex: { instance_id: instanceId },
      }),
    ),
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

  it("offers no second Plex entry, because the account is one", async () => {
    // Every Plex screen drives the same account flow and the same destination
    // row, so a second entry was two screens for one server: the second server
    // selection would overwrite the first rather than connect anything.
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("checkbox", { name: /^Plex$/ }));

    expect(
      screen.queryByRole("button", { name: /add another plex/i }),
    ).toBeNull();
    expect(
      screen.getByText(/one plex account per install/i),
    ).toBeInTheDocument();

    // Every other kind still takes as many as the reader wants.
    await user.click(screen.getByRole("checkbox", { name: /^Emby$/ }));
    expect(
      screen.getByRole("button", { name: /add another emby/i }),
    ).toBeInTheDocument();
  });

  it("signs the Plex account out before deleting the row it owns", async () => {
    // Deleting the row on its own leaves the token, the chosen server and the
    // recorded owner id in the Plex settings, and the next account reconcile
    // or startup builds the supposedly disconnected server straight back.
    const calls: string[] = [];
    setInstances({ plex: [row("plex", "Plex", "plex-1")] });
    setPlexOwner("plex-1");
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-1", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: /^disconnect$/i }),
    );
    expect(
      screen.getByText(/signed out of your plex account/i),
    ).toBeInTheDocument();
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[0],
    );

    await waitFor(() => expect(calls).toEqual(["logout", "delete"]));
    // Signing out clears use_plex itself, so the step writes no switch of its
    // own on top of it.
    expect(mutate).not.toHaveBeenCalled();
  });

  it("deletes a hand-added Plex row without touching the account", async () => {
    const calls: string[] = [];
    setInstances({
      plex: [row("plex", "Plex", "plex-1"), row("plex", "Loft", "plex-2")],
    });
    setPlexOwner("plex-1");
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-2", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await screen.findByText("Loft");
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[1],
    );
    expect(screen.queryByText(/signed out of your plex account/i)).toBeNull();
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[1],
    );

    await waitFor(() => expect(calls).toEqual(["delete"]));
  });

  it("waits for the settings before offering to disconnect a Plex row", async () => {
    // Which Plex row the account owns comes from the settings. While that query
    // is still in flight the missing value reads as "nothing recorded", which
    // is indistinguishable from an account that owns no row, so the account's
    // own row could be deleted with the token left behind for the next
    // reconcile to rebuild it from.
    let release: (() => void) | undefined;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    setInstances({
      plex: [row("plex", "Plex", "plex-1"), row("plex", "Loft", "plex-2")],
    });
    server.use(
      http.get("/api/system/settings", async () => {
        await held;
        return HttpResponse.json({
          general: {},
          translator: {},
          // eslint-disable-next-line camelcase
          plex: { instance_id: "plex-2" },
        });
      }),
    );
    withSelection(<MediaServerStep onNext={onNext} />);

    const waiting = await screen.findAllByRole("button", {
      name: /^disconnect$/i,
    });
    expect(waiting[0]).toBeDisabled();

    release?.();

    await waitFor(() =>
      expect(
        screen.getAllByRole("button", { name: /^disconnect$/i })[0],
      ).toBeEnabled(),
    );
  });

  it("owns no Plex row when the recorded id matches none", async () => {
    // The backend adopts no row it did not create
    // (media_servers/plex_account.py::_account_row), so a recorded id that
    // matches nothing means the account owns nothing. Treating the only row
    // there is as the account's signed the reader out over a server they had
    // added by hand.
    const calls: string[] = [];
    setInstances({ plex: [row("plex", "Loft", "plex-2")] });
    setPlexOwner("plex-deleted");
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-2", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(
      await screen.findByRole("button", { name: /^disconnect$/i }),
    );
    expect(screen.queryByText(/signed out of your plex account/i)).toBeNull();
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[0],
    );

    await waitFor(() => expect(calls).toEqual(["delete"]));
  });

  it("leaves the Plex card open when the account owns no row", async () => {
    // The card is the only way to reach the Plex account sign-in: there is no
    // "Add another Plex" beside it. Locking it because some Plex row exists
    // left a reader who had added one by hand in Connections unable to sign in
    // at all without deleting a server they never asked about.
    setInstances({ plex: [row("plex", "Loft", "plex-2")] });
    setPlexOwner("plex-deleted");
    withSelection(<MediaServerStep onNext={onNext} />);

    await screen.findByText("Loft");
    const checkbox = screen.getByRole("checkbox", { name: /^Plex$/ });
    await waitFor(() => expect(checkbox).toBeEnabled());
    expect(checkbox).not.toBeChecked();
  });

  it("locks the Plex card once the account owns a row", async () => {
    setInstances({ plex: [row("plex", "Plex", "plex-1")] });
    setPlexOwner("plex-1");
    withSelection(<MediaServerStep onNext={onNext} />);

    await screen.findByText("Plex");
    const checkbox = screen.getByRole("checkbox", { name: /^Plex$/ });
    await waitFor(() => expect(checkbox).toBeDisabled());
    expect(checkbox).toBeChecked();
  });

  it("does not call a switched-off row connected", async () => {
    // The dispatcher skips a disabled instance, and the backend keeps exactly
    // one after a Plex sign-out, credential cleared. Reading it as connected
    // ticked and locked the card, so the reader was told a refresh destination
    // was set up and given no way to set one up at all.
    setInstances({
      jellyfin: [{ ...row("jellyfin", "Attic", "jf-1"), enabled: false }],
    });
    withSelection(<MediaServerStep onNext={onNext} />);

    expect(await screen.findByText("Attic")).toBeInTheDocument();
    expect(screen.getByText(/turned off/i)).toBeInTheDocument();
    expect(screen.queryByText("Connected")).toBeNull();

    const checkbox = screen.getByRole("checkbox", { name: /^Jellyfin$/ });
    expect(checkbox).not.toBeChecked();
    expect(checkbox).toBeEnabled();
  });

  it("keeps Plex refreshing for the servers the reader kept", async () => {
    // Signing out clears use_plex for the whole kind, and the dispatcher reads
    // that switch before any row. Deleting only the account's destination
    // therefore left the hand-added Plex servers standing but silent.
    const calls: string[] = [];
    setInstances({
      plex: [row("plex", "Plex", "plex-1"), row("plex", "Loft", "plex-2")],
    });
    setPlexOwner("plex-1");
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-1", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await screen.findByText("Loft");
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[0],
    );
    await user.click(
      screen.getAllByRole("button", { name: /^disconnect$/i })[0],
    );

    await waitFor(() => expect(calls).toEqual(["logout", "delete"]));
    await waitFor(() =>
      expect(mutate).toHaveBeenCalledWith({
        "settings-general-use_plex": true,
      }),
    );
  });

  it("unticking a kind drops the servers it added", async () => {
    const user = userEvent.setup();
    withSelection(<MediaServerStep onNext={onNext} />);

    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));
    expect(screen.getByText("1 to set up")).toBeInTheDocument();
    // One pending server needs no Remove of its own: unticking the card is
    // how it is undone, and a second control for the same thing is noise.
    expect(screen.queryByRole("button", { name: "Remove Silo" })).toBeNull();

    await user.click(screen.getByRole("checkbox", { name: /^Silo$/ }));

    expect(screen.queryByText("1 to set up")).toBeNull();
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
