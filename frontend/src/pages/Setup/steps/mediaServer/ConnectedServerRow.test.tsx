/* eslint-disable camelcase */

import { showNotification } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import ConnectedServerRow from "./ConnectedServerRow";

// Only the call is replaced: the provider tree renders the real Notifications
// and other modules in it reach for the rest of this module.
vi.mock("@mantine/notifications", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@mantine/notifications")>();
  return { ...actual, showNotification: vi.fn() };
});

const shown = vi.mocked(showNotification);

function row(
  kind: MediaServerKind,
  name: string,
  id: string,
): MediaServerInstance {
  return {
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
  };
}

/** Open the confirmation and answer it. */
async function disconnect(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /^disconnect$/i }));
  await user.click(screen.getAllByRole("button", { name: /^disconnect$/i })[0]);
}

describe("ConnectedServerRow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("says so when the switch the Plex sign-out cleared cannot be put back", async () => {
    // Signing out clears use_plex for the whole kind, so the Plex servers the
    // reader added by hand stop refreshing with the account's own. The write
    // that puts the switch back was sent and forgotten: when it failed those
    // servers stayed silent and the row still reported a clean disconnect.
    const calls: string[] = [];
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-1", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
      http.post("/api/system/settings", () => {
        calls.push("settings");
        return new HttpResponse(null, { status: 500 });
      }),
    );
    const onDisconnected = vi.fn();
    const user = userEvent.setup();

    customRender(
      <ConnectedServerRow
        instance={row("plex", "Plex", "plex-1")}
        kind="plex"
        last={false}
        accountOwned
        onDisconnected={onDisconnected}
      />,
    );

    await disconnect(user);

    await waitFor(() =>
      expect(calls).toEqual(["logout", "delete", "settings"]),
    );
    await waitFor(() =>
      expect(shown).toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringContaining("are not refreshing"),
        }),
      ),
    );
  });

  it("keeps quiet when the switch goes back on", async () => {
    const calls: string[] = [];
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-1", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
      http.post("/api/system/settings", () => {
        calls.push("settings");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const onDisconnected = vi.fn();
    const user = userEvent.setup();

    customRender(
      <ConnectedServerRow
        instance={row("plex", "Plex", "plex-1")}
        kind="plex"
        last={false}
        accountOwned
        onDisconnected={onDisconnected}
      />,
    );

    await disconnect(user);

    await waitFor(() => expect(onDisconnected).toHaveBeenCalledWith("plex-1"));
    expect(calls).toEqual(["logout", "delete", "settings"]);
    expect(
      shown.mock.calls.filter(([data]) =>
        /are not refreshing/.test(String(data.message)),
      ),
    ).toHaveLength(0);
  });

  it("leaves Plex refreshes off when the reader had turned them off", async () => {
    // The switch is put back because signing out cleared it, not because a
    // disconnect is a reason to start refreshing. A reader who had Plex
    // refreshes off keeps them off, and the prop cannot be read after the
    // sign-out: that is what cleared it.
    const calls: string[] = [];
    server.use(
      http.post("/api/plex/oauth/logout", () => {
        calls.push("logout");
        return HttpResponse.json({ success: true });
      }),
      http.delete("/api/system/media-server-instances/plex-1", () => {
        calls.push("delete");
        return new HttpResponse(null, { status: 204 });
      }),
      http.post("/api/system/settings", () => {
        calls.push("settings");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const onDisconnected = vi.fn();
    const user = userEvent.setup();

    customRender(
      <ConnectedServerRow
        instance={row("plex", "Plex", "plex-1")}
        kind="plex"
        last={false}
        accountOwned
        kindEnabled={false}
        onDisconnected={onDisconnected}
      />,
    );

    await disconnect(user);

    await waitFor(() => expect(onDisconnected).toHaveBeenCalledWith("plex-1"));
    expect(calls).toEqual(["logout", "delete"]);
  });

  it("finishes the disconnect the reader walked away from", async () => {
    // TanStack drops a mutate call's own callbacks once the component that
    // made the call has unmounted, which pressing Back or Skip mid-disconnect
    // does. The delete still ran, so the row was gone while the draft that
    // wrote it stayed in the wizard and the picker went on counting it.
    let release: () => void = () => undefined;
    const blocked = new Promise<void>((resolve) => {
      release = resolve;
    });
    const started = vi.fn();
    server.use(
      http.delete("/api/system/media-server-instances/emby-1", async () => {
        started();
        await blocked;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const onDisconnected = vi.fn();
    const user = userEvent.setup();

    const { unmount } = customRender(
      <ConnectedServerRow
        instance={row("emby", "Living room", "emby-1")}
        kind="emby"
        last={false}
        onDisconnected={onDisconnected}
      />,
    );

    await disconnect(user);
    await waitFor(() => expect(started).toHaveBeenCalled());

    unmount();
    release();

    await waitFor(() => expect(onDisconnected).toHaveBeenCalledWith("emby-1"));
  });
});
