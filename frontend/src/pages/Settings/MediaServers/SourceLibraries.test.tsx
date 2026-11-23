/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import type { MediaSource, SourceLibrary } from "@/apis/raw/mediaSources";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const sourcesUrl = "/api/system/media-sources";
const serversUrl = "/api/system/media-server-instances";
const connectionId = "2af88684-d7d2-4534-82bb-a6d839cc5c10";
const siblingId = "66e883df-68c7-4d66-8217-8fc206c5a563";
const movieLibrary: SourceLibrary = {
  id: 6,
  locator: "movies",
  name: "Cinema",
  content_kind: "movie",
  profile_id: null,
  enabled: true,
  last_scan_status: null,
  last_scan_message: null,
  roots: [{ server_path: "/server/movies", local_path: "/local/movies" }],
};

function source(
  kind: "jellyfin" | "emby",
  instanceId = connectionId,
): MediaSource {
  return {
    id: instanceId === connectionId ? 12 : 18,
    kind,
    name: "Living room",
    enabled: true,
    scan_frequency_hours: 12,
    media_server_instance_id: instanceId,
    libraries: [movieLibrary],
  };
}

function setup(
  kind: MediaServerKind,
  initial: MediaSource[] = [],
  visible = true,
) {
  let connection: MediaServerInstance | undefined = {
    id: connectionId,
    kind,
    name: "Living room",
    enabled: true,
    url: `https://${kind}.example`,
    verify_ssl: true,
    api_key_set: true,
    path_mappings: [],
    refresh_movies: true,
    refresh_episodes: true,
    options: {},
    external_url: "",
  };
  let sources = initial;
  const reads: string[] = [];
  window.history.replaceState(null, "", `/#${kind}`);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", [`use_${kind}`]: true },
        media_sources: { visible },
      }),
    ),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { bazarr_version: "2.8.0" } }),
    ),
    http.get(serversUrl, () =>
      HttpResponse.json({ data: connection ? [connection] : [] }),
    ),
    http.get(`${serversUrl}/:id/status`, () =>
      HttpResponse.json({ pending: 0, state: "idle", error_code: null }),
    ),
    http.get(sourcesUrl, () => {
      reads.push("source list");
      return HttpResponse.json({ data: sources });
    }),
    http.get(`${sourcesUrl}/server-libraries`, ({ request }) => {
      reads.push(
        new URL(request.url).searchParams.get("media_server_instance_id") ?? "",
      );
      return HttpResponse.json({
        data: [
          {
            id: "movies",
            name: "Cinema",
            content_kind: "movie",
            roots: [{ server_path: "/server/movies", local_path: null }],
          },
          {
            id: "events",
            name: "Events",
            content_kind: null,
            roots: [{ server_path: "D:\\Events", local_path: null }],
          },
        ],
      });
    }),
  );
  customRender(<SettingsConnectionsView />);
  return {
    reads,
    setSources: (next: MediaSource[]) => (sources = next),
    removeConnection: () => (connection = undefined),
  };
}

afterEach(() => window.history.replaceState(null, "", "/"));

it.each(["jellyfin", "emby"] as const)(
  "uses the saved %s instance to add, map and scan its own server libraries",
  async (kind) => {
    const other = { ...source(kind, siblingId), libraries: [movieLibrary] };
    const fixture = setup(kind, [other]);
    const writes: unknown[] = [];
    const sportsLibrary: SourceLibrary = {
      ...movieLibrary,
      id: 8,
      locator: "events",
      name: "Events",
      content_kind: "sports",
      roots: [{ server_path: "D:\\Events", local_path: "/local/events" }],
    };
    server.use(
      http.post(sourcesUrl, async ({ request }) => {
        writes.push(await request.json());
        const created = source(kind);
        fixture.setSources([other, created]);
        return HttpResponse.json(created, { status: 201 });
      }),
      http.post(`${sourcesUrl}/12/libraries`, async ({ request }) => {
        writes.push(await request.json());
        fixture.setSources([
          other,
          { ...source(kind), libraries: [movieLibrary, sportsLibrary] },
        ]);
        return HttpResponse.json(sportsLibrary, { status: 201 });
      }),
      http.post(`${sourcesUrl}/12/scan`, () => {
        writes.push("scan");
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );

    await userEvent.click(
      await screen.findByRole("button", { name: "Use as a library source" }),
    );
    const first = within(await screen.findByRole("dialog"));
    await first.findByRole("option", { name: "Cinema" });
    await userEvent.selectOptions(
      first.getByRole("combobox", { name: "Server library" }),
      "movies",
    );
    expect(first.getByLabelText("Server path 1")).toHaveValue("/server/movies");
    expect(first.getByRole("combobox", { name: "Content kind" })).toHaveValue(
      "movie",
    );
    const local = first.getByLabelText("Local path 1");
    await userEvent.type(local, "media/movies");
    expect(first.getByText("Enter an absolute path.")).toBeInTheDocument();
    expect(first.getByRole("button", { name: "Add source" })).toBeDisabled();
    await userEvent.clear(local);
    await userEvent.type(local, "/local/movies");
    await userEvent.click(first.getByRole("button", { name: "Add root" }));
    await userEvent.type(
      first.getByLabelText("Server path 2"),
      "/server/other",
    );
    expect(first.getByRole("button", { name: "Add source" })).toBeDisabled();
    await userEvent.click(first.getByRole("button", { name: "Remove root 2" }));
    await userEvent.click(first.getByRole("button", { name: "Add source" }));
    await screen.findByText("Cinema");
    expect(writes).toEqual([
      {
        kind,
        media_server_instance_id: connectionId,
        name: "Living room",
        library: {
          server_library_id: "movies",
          content_kind: "movie",
          profile_id: null,
          roots: movieLibrary.roots,
        },
      },
    ]);
    expect(fixture.reads).toContain(connectionId);
    expect(fixture.reads).not.toContain(siblingId);

    await userEvent.click(screen.getByRole("button", { name: "Add library" }));
    const second = within(await screen.findByRole("dialog"));
    await second.findByRole("option", { name: "Events" });
    expect(second.queryByRole("option", { name: "Cinema" })).toBeNull();
    await userEvent.selectOptions(
      second.getByRole("combobox", { name: "Server library" }),
      "events",
    );
    await userEvent.type(
      second.getByLabelText("Local path 1"),
      "/local/events",
    );
    expect(second.getByRole("button", { name: "Add library" })).toBeDisabled();
    await userEvent.selectOptions(
      second.getByRole("combobox", { name: "Content kind" }),
      "sports",
    );
    await userEvent.click(second.getByRole("button", { name: "Add library" }));
    await screen.findByText("Events");
    expect(writes[1]).toEqual({
      server_library_id: "events",
      content_kind: "sports",
      profile_id: null,
      roots: sportsLibrary.roots,
    });
    await userEvent.click(
      screen.getByRole("button", { name: "Scan libraries" }),
    );
    await screen.findByText("Library scan requested.");
    expect(writes[2]).toBe("scan");
  },
);

it("validates edited roots on the server and requires fresh confirmation after a count change", async () => {
  const original = source("jellyfin");
  const fixture = setup("jellyfin", [original]);
  const endpoint = `${sourcesUrl}/12/libraries/6`;
  const previewed: unknown[] = [];
  const applied: unknown[] = [];
  let validPath = false;
  let count = 3;
  const changedRoots = [
    { server_path: "/server/movies", local_path: "/local/replacement" },
  ];
  server.use(
    http.post(`${endpoint}/roots/preview`, async ({ request }) => {
      previewed.push(await request.json());
      return validPath
        ? HttpResponse.json({ removed_items: count })
        : HttpResponse.json(
            { message: "This folder overlaps another library." },
            { status: 409 },
          );
    }),
    http.patch(endpoint, async ({ request }) => {
      const body = (await request.json()) as { confirmed_removed: number };
      applied.push(body);
      if (body.confirmed_removed !== count) {
        return HttpResponse.json(
          {
            message:
              "This change removes 1 item. Preview the change and confirm that count.",
          },
          { status: 409 },
        );
      }
      const library = { ...movieLibrary, roots: changedRoots };
      fixture.setSources([{ ...original, libraries: [library] }]);
      return HttpResponse.json(library);
    }),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "Edit roots" }),
  );
  const dialog = within(await screen.findByRole("dialog"));
  const local = dialog.getByLabelText("Local path 1");
  expect(local).toHaveValue("/local/movies");
  await userEvent.clear(local);
  await userEvent.type(local, "/local/replacement");
  await userEvent.click(
    dialog.getByRole("button", { name: "Preview changes" }),
  );
  await dialog.findByText("This folder overlaps another library.");
  expect(applied).toEqual([]);
  validPath = true;
  await userEvent.click(
    dialog.getByRole("button", { name: "Preview changes" }),
  );
  await dialog.findByText(
    "These root changes will remove 3 indexed items from this library.",
  );
  expect(
    dialog.getByText(
      /Subtitle history, blacklist entries, and files on disk are kept/,
    ),
  ).toBeInTheDocument();
  await userEvent.click(dialog.getByRole("button", { name: "Back" }));
  expect(dialog.getByLabelText("Local path 1")).toHaveValue(
    "/local/replacement",
  );
  expect(applied).toEqual([]);
  await userEvent.click(
    dialog.getByRole("button", { name: "Preview changes" }),
  );
  await dialog.findByText(/remove 3 indexed items/);
  count = 1;
  await userEvent.click(
    dialog.getByRole("button", { name: "Apply root changes" }),
  );
  await dialog.findByText(/Preview the change and confirm that count/);
  expect(
    dialog.queryByRole("button", { name: "Apply root changes" }),
  ).toBeNull();
  expect(previewed).toHaveLength(3);
  await userEvent.click(
    dialog.getByRole("button", { name: "Preview changes" }),
  );
  await dialog.findByText(/remove 1 indexed item from this library/);
  await userEvent.click(
    dialog.getByRole("button", { name: "Apply root changes" }),
  );
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(await screen.findByText("/local/replacement")).toBeInTheDocument();
  expect(applied).toEqual([
    { roots: changedRoots, confirmed_removed: 3 },
    { roots: changedRoots, confirmed_removed: 1 },
  ]);
});

it.each(["jellyfin", "emby"] as const)(
  "discloses linked %s source removal before deleting the saved connection",
  async (kind) => {
    const fixture = setup(kind, [source(kind)]);
    let deleted = 0;
    server.use(
      http.delete(`${serversUrl}/${connectionId}`, () => {
        deleted++;
        fixture.setSources([]);
        fixture.removeConnection();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    await screen.findByText("Cinema");
    const before = fixture.reads.length;
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    let dialog = within(await screen.findByRole("dialog"));
    expect(
      dialog.getByText(
        /removes any linked library source and its indexed items/,
      ),
    ).toBeInTheDocument();
    expect(
      dialog.getByText(
        /Subtitle history, blacklist entries, and files on disk are kept/,
      ),
    ).toBeInTheDocument();
    await userEvent.click(dialog.getByRole("button", { name: "Cancel" }));
    expect(deleted).toBe(0);
    await userEvent.click(screen.getByRole("button", { name: "Delete" }));
    dialog = within(await screen.findByRole("dialog"));
    await userEvent.click(
      dialog.getByRole("button", { name: "Delete instance" }),
    );
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(
      await screen.findByText(/No (Jellyfin|Emby) instances yet/),
    ).toBeInTheDocument();
    expect(deleted).toBe(1);
    expect(fixture.reads.length).toBeGreaterThan(before);
  },
);

it("leaves the normal Jellyfin connection form without root inputs and respects the media-source setting", async () => {
  const fixture = setup("jellyfin", [source("jellyfin")], false);
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const dialog = within(await screen.findByRole("dialog"));
  expect(dialog.getByLabelText("Server URL")).toHaveValue(
    "https://jellyfin.example",
  );
  expect(dialog.queryByText(/path mappings|library roots/i)).toBeNull();
  expect(dialog.queryByLabelText(/local path|server path/i)).toBeNull();
  expect(
    screen.queryByRole("button", { name: "Use as a library source" }),
  ).toBeNull();
  expect(fixture.reads).toEqual([]);
});
