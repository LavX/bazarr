/* eslint-disable camelcase */

import { showNotification } from "@mantine/notifications";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { makeInstance } from "./__tests__/fixtures";
import SettingsConnectionsView from "./index";

// A spy that still shows the toast, so the page behaves as it does for real.
vi.mock("@mantine/notifications", async (importOriginal) => {
  const actual =
    await importOriginal<typeof import("@mantine/notifications")>();
  return { ...actual, showNotification: vi.fn(actual.showNotification) };
});

describe("Connections page", () => {
  beforeEach(() =>
    server.use(
      http.get("/api/system/media-server-instances", () =>
        HttpResponse.json({ data: [] }),
      ),
    ),
  );
  afterEach(() => window.history.replaceState(null, "", "/"));

  it.each(["emby", "silo"])("opens %s directly from its hash", async (kind) => {
    window.history.replaceState(null, "", `/#${kind}`);
    customRender(<SettingsConnectionsView />);
    expect(
      await screen.findByRole("tab", { name: new RegExp(kind, "i") }),
    ).toHaveAttribute("aria-selected", "true");
    expect(
      screen.getByText(`Use ${kind === "emby" ? "Emby" : "Silo"}`),
    ).toBeInTheDocument();
  });
  it("renders a tab for each service", async () => {
    customRender(<SettingsConnectionsView />);
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: /sonarr/i })).toBeInTheDocument();
    });
    expect(screen.getByRole("tab", { name: /radarr/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /sportarr/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /plex/i })).toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /jellyfin/i })).toBeInTheDocument();
  });

  it("shows Sonarr global options on the default tab (lossless fold)", async () => {
    // Enable Sonarr so the CollapseBox is open and its children are in the DOM.
    // Mantine Collapse with transitionDuration={0} returns null (not mounted)
    // when expanded=false, so this override is required to reach the gated options.
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { theme: "auto", use_sonarr: true } }),
      ),
    );
    customRender(<SettingsConnectionsView />);
    // Master toggle relocated from the old Sonarr page.
    await waitFor(() => {
      expect(screen.getByText("Use Sonarr")).toBeInTheDocument();
    });
    // A representative global option that is NOT part of the instance card form.
    await waitFor(() => {
      expect(screen.getByText("Download Only Monitored")).toBeInTheDocument();
    });
  });

  it("switches to the Plex tab and shows its instance list", async () => {
    const user = userEvent.setup();
    customRender(<SettingsConnectionsView />);
    await user.click(await screen.findByRole("tab", { name: /plex/i }));
    await waitFor(() => {
      expect(screen.getByText("Use Plex")).toBeInTheDocument();
    });
    expect(await screen.findByText("Plex instances")).toBeInTheDocument();
  });
});

it("switches to Jellyfin and shows its instance list", async () => {
  customRender(<SettingsConnectionsView />);
  await userEvent.click(await screen.findByRole("tab", { name: "Jellyfin" }));
  expect(await screen.findByText("Use Jellyfin")).toBeInTheDocument();
  expect(await screen.findByText("Jellyfin instances")).toBeInTheDocument();
  expect(screen.getByRole("switch", { name: "Enabled" })).toBeInTheDocument();
});

describe("deleting an instance that owns a synced library", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  const shared = { history: 4, blacklist: 5, root_folders: 1 };
  const libraries = {
    sonarr: { series: 1, episodes: 2, movies: 0, ...shared },
    radarr: { series: 0, episodes: 0, movies: 3, ...shared },
  };

  function serveInstance(kind: "sonarr" | "radarr", lastOfKind = true) {
    const instance = makeInstance({
      id: 7,
      kind,
      name: `Old ${kind}`,
      display_name: `Old ${kind}`,
      is_default: true,
    });
    const requests: string[] = [];
    let saved = [instance];
    let syncRunning = false;
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", use_sonarr: true, use_radarr: true },
        }),
      ),
      http.get("/api/system/arr-instances", () => HttpResponse.json(saved)),
      http.delete("/api/system/arr-instances/7", ({ request }) => {
        const url = new URL(request.url);
        requests.push(url.search);
        if (url.searchParams.get("remove_library") !== "true") {
          return HttpResponse.json(
            {
              error: "conflict",
              message: "cannot delete an instance that still owns rows",
              can_remove_library: true,
              library: libraries[kind],
              last_of_kind: lastOfKind,
            },
            { status: 409 },
          );
        }
        if (syncRunning) {
          return HttpResponse.json(
            {
              error: "sync_in_progress",
              message:
                "A library sync of this instance is running or queued. Wait for it to finish, then delete the instance again.",
            },
            { status: 409 },
          );
        }
        saved = [];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    window.history.replaceState(null, "", `/#${kind}`);
    return {
      requests,
      setSyncRunning: (value: boolean) => {
        syncRunning = value;
      },
    };
  }

  async function openDelete(
    user: ReturnType<typeof userEvent.setup>,
    name: string,
  ) {
    // The settings query re-renders the section when it resolves, which would
    // close a card menu opened before it.
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked(),
    );
    await user.click(
      await screen.findByRole("button", { name: `More actions for ${name}` }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    return screen.findByRole("dialog", { name: "Delete instance" });
  }

  it.each([
    ["sonarr", "Sonarr", ["1 series", "2 episodes"], "0 movies"],
    ["radarr", "Radarr", ["3 movies"], "0 series"],
  ] as const)(
    "offers %s library removal only after the refusal, behind a second confirmation",
    async (kind, label, media, empty) => {
      const user = userEvent.setup();
      const { requests } = serveInstance(kind);
      customRender(<SettingsConnectionsView />);

      const modal = await openDelete(user, `Old ${kind}`);
      expect(modal).toHaveTextContent(
        `Delete Old ${kind} (${label})? Its connection settings will be removed.`,
      );
      expect(
        within(modal).queryByRole("button", {
          name: "Delete with its synced library",
        }),
      ).toBeNull();

      await user.click(
        within(modal).getByRole("button", { name: "Delete instance" }),
      );

      const offer = await within(modal).findByRole("button", {
        name: "Delete with its synced library",
      });
      expect(requests).toEqual([""]);
      expect(modal).not.toHaveTextContent(/remove or reassign/i);
      expect(modal).toHaveTextContent(
        "This instance still has a synced library",
      );
      expect(modal).toHaveTextContent(
        `Bazarr+ still holds ${media.join(", ")}, 4 history entries, 5 exclusion records and 1 root folder record from ${label}, so its last instance cannot be deleted on its own.`,
      );
      // The counts are every record of the kind, not only what it synced.
      expect(modal).not.toHaveTextContent("synced from it");
      // A plain delete would only be refused again.
      expect(
        within(modal).queryByRole("button", { name: "Delete instance" }),
      ).toBeNull();

      await user.click(offer);

      const confirm = await screen.findByRole("dialog", {
        name: "Delete instance and its library",
      });
      for (const item of [
        ...media,
        "4 history entries",
        "5 exclusion records",
        "1 root folder record",
      ]) {
        expect(within(confirm).getByText(item)).toBeInTheDocument();
      }
      expect(within(confirm).queryByText(empty)).toBeNull();
      expect(confirm).toHaveTextContent(
        "Video and subtitle files on disk are not touched",
      );
      expect(confirm).toHaveTextContent(
        `Delete Old ${kind} (${label}) together with every ${label} record Bazarr+ holds:`,
      );
      expect(confirm).toHaveTextContent(
        `This is your last ${label} instance, so Use ${label} is switched off too. Switch it back on when you add a new instance.`,
      );
      expect(requests).toEqual([""]);

      await user.click(
        within(confirm).getByRole("button", {
          name: "Delete instance and library",
        }),
      );

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(requests).toEqual(["", "?remove_library=true"]);
      await waitFor(() => expect(screen.queryByText(`Old ${kind}`)).toBeNull());
    },
  );

  it("goes back without deleting and says why a running sync refuses", async () => {
    const user = userEvent.setup();
    const { requests, setSyncRunning } = serveInstance("sonarr", false);
    customRender(<SettingsConnectionsView />);

    const modal = await openDelete(user, "Old sonarr");
    await user.click(
      within(modal).getByRole("button", { name: "Delete instance" }),
    );
    const offer = await within(modal).findByRole("button", {
      name: "Delete with its synced library",
    });
    expect(modal).toHaveTextContent(
      "Bazarr+ still holds 1 series, 2 episodes, 4 history entries, 5 exclusion records and 1 root folder record synced from it, so it cannot be deleted on its own.",
    );
    await user.click(offer);
    const confirm = await screen.findByRole("dialog", {
      name: "Delete instance and its library",
    });
    expect(confirm).toHaveTextContent(
      "Delete Old sonarr (Sonarr) together with everything Bazarr+ synced from it:",
    );
    // Another Sonarr instance remains, so nothing is switched off.
    expect(confirm).not.toHaveTextContent("switched off");

    await user.click(within(confirm).getByRole("button", { name: "Back" }));
    expect(
      await screen.findByRole("dialog", { name: "Delete instance" }),
    ).toBeInTheDocument();
    expect(requests).toEqual([""]);

    setSyncRunning(true);
    await user.click(
      await screen.findByRole("button", {
        name: "Delete with its synced library",
      }),
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Delete instance and library",
      }),
    );

    expect(
      await screen.findByText(/A library sync of this instance is running/),
    ).toBeInTheDocument();
    expect(requests).toEqual(["", "?remove_library=true"]);
    expect(
      screen.getByRole("dialog", { name: "Delete instance and its library" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "More actions for Old sonarr" }),
    ).toBeInTheDocument();
  });
});

describe("deleting the last instance of a kind", () => {
  afterEach(() => {
    window.history.replaceState(null, "", "/");
    vi.mocked(showNotification).mockClear();
  });

  function serve(kind: "sonarr" | "radarr", withSibling: boolean) {
    let saved = [
      makeInstance({ id: 7, kind, name: "Only", display_name: "Only" }),
      ...(withSibling
        ? [makeInstance({ id: 8, kind, name: "Other", display_name: "Other" })]
        : []),
    ];
    let kindOn = true;
    const settingsReads = { count: 0 };
    server.use(
      http.get("/api/system/settings", () => {
        settingsReads.count += 1;
        return HttpResponse.json({
          general: { theme: "auto", [`use_${kind}`]: kindOn },
        });
      }),
      http.get("/api/system/arr-instances", () => HttpResponse.json(saved)),
      http.delete("/api/system/arr-instances/7", () => {
        saved = saved.filter((instance) => instance.id !== 7);
        // What the server does when the kind has no instance left.
        kindOn = saved.length > 0;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    window.history.replaceState(null, "", `/#${kind}`);
    return settingsReads;
  }

  async function openDelete(user: ReturnType<typeof userEvent.setup>) {
    await waitFor(() =>
      expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked(),
    );
    await user.click(
      await screen.findByRole("button", { name: "More actions for Only" }),
    );
    await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
    return screen.findByRole("dialog", { name: "Delete instance" });
  }

  it.each([
    ["sonarr", "Sonarr"],
    ["radarr", "Radarr"],
  ] as const)(
    "says a plain delete of the only %s instance switches it off",
    async (kind, label) => {
      const user = userEvent.setup();
      const settingsReads = serve(kind, false);
      customRender(<SettingsConnectionsView />);

      const modal = await openDelete(user);
      expect(modal).toHaveTextContent(
        `This is your last ${label} instance, so Use ${label} is switched off too. Switch it back on when you add a new instance.`,
      );
      const readsBefore = settingsReads.count;

      await user.click(
        within(modal).getByRole("button", { name: "Delete instance" }),
      );

      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      expect(showNotification).toHaveBeenCalledWith(
        expect.objectContaining({ message: 'Instance "Only" removed' }),
      );
      // The dialog said it would. Whether it did, the switch shows once the
      // settings are read again, so the toast does not claim it.
      expect(showNotification).not.toHaveBeenCalledWith(
        expect.objectContaining({
          message: expect.stringContaining("switched off"),
        }),
      );
      // The switch follows at once, not only when a socket event arrives.
      await waitFor(() =>
        expect(settingsReads.count).toBeGreaterThan(readsBefore),
      );
      await waitFor(() =>
        expect(
          screen.getByRole("switch", { name: "Enabled" }),
        ).not.toBeChecked(),
      );
    },
  );

  it("says nothing about switching off while another instance remains", async () => {
    const user = userEvent.setup();
    serve("sonarr", true);
    customRender(<SettingsConnectionsView />);

    const modal = await openDelete(user);
    expect(modal).not.toHaveTextContent("switched off");

    await user.click(
      within(modal).getByRole("button", { name: "Delete instance" }),
    );

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(showNotification).toHaveBeenCalledWith(
      expect.objectContaining({ message: 'Instance "Only" removed' }),
    );
    expect(screen.getByRole("switch", { name: "Enabled" })).toBeChecked();
  });
});
