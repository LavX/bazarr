/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import type { MediaServerInstance } from "@/apis/raw/mediaServers";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const id = "2af88684-d7d2-4534-82bb-a6d839cc5c10";
const item = `/api/system/media-server-instances/${id}`;

function instance(
  kind: "jellyfin" | "plex",
  overrides: Partial<MediaServerInstance> = {},
): MediaServerInstance {
  return {
    id,
    kind,
    name: "Living room",
    enabled: true,
    url: `https://${kind}.example`,
    verify_ssl: true,
    api_key_set: true,
    // Neither kind resolves by path, so neither has mappings to carry.
    path_mappings: [],
    refresh_movies: true,
    refresh_episodes: true,
    options:
      kind === "jellyfin"
        ? { movie_library_ids: ["lib-movies"], refresh_method: "immediate" }
        : { movie_libraries: ["Films"] },
    ...overrides,
  };
}

function setup(kind: "jellyfin" | "plex", rows = [instance(kind)]) {
  window.history.replaceState(null, "", `/#${kind}`);
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", [`use_${kind}`]: true },
      }),
    ),
    http.get("/api/system/media-server-instances", () =>
      HttpResponse.json({ data: rows }),
    ),
    http.get("/api/system/media-server-instances/:id/status", () =>
      HttpResponse.json({ pending: 0, state: "idle", error_code: null }),
    ),
  );
  customRender(<SettingsConnectionsView />);
}

afterEach(() => window.history.replaceState(null, "", "/"));

it.each(["jellyfin", "plex"] as const)(
  "%s instances are edited without any path mapping field at all",
  async (kind) => {
    setup(kind);
    await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
    const modal = within(await screen.findByRole("dialog"));
    expect(modal.getByLabelText("Name")).toHaveValue("Living room");
    // The regression this convergence must not introduce: these two have never
    // needed a path mapping and the form must not start asking for one.
    expect(modal.queryByText("Path mappings")).not.toBeInTheDocument();
    expect(modal.queryByLabelText("Local path 1")).not.toBeInTheDocument();
    expect(modal.getByRole("switch", { name: "Refresh movies" })).toBeChecked();
    expect(
      modal.getByRole("switch", { name: "Refresh episodes" }),
    ).toBeChecked();
  },
);

it("keeps a Plex tab that still carries the account settings", async () => {
  setup("plex");
  expect(await screen.findByText("Plex instances")).toBeInTheDocument();
  // The recently-added dates are not refreshes and still read the scalars.
  expect(
    await screen.findByText(
      "Mark movies as recently added after downloading subtitles",
    ),
  ).toBeInTheDocument();
});

it("saves a Jellyfin instance with its libraries and refresh method", async () => {
  const writes: LooseObject[] = [];
  setup("jellyfin", []);
  server.use(
    http.post(`${item}/libraries`, () =>
      HttpResponse.json({
        data: [
          { id: "lib-movies", name: "Films", type: "movies" },
          { id: "lib-shows", name: "Shows", type: "tvshows" },
        ],
        error_code: null,
      }),
    ),
    http.post("/api/system/media-server-instances", async ({ request }) => {
      const body = (await request.json()) as LooseObject;
      writes.push(body);
      return HttpResponse.json(
        instance("jellyfin", { ...body, api_key_set: true }),
      );
    }),
  );
  await screen.findByText("No Jellyfin instances yet");
  await userEvent.click(screen.getByRole("button", { name: "Add instance" }));
  const modal = within(await screen.findByRole("dialog"));
  await userEvent.type(modal.getByLabelText("Name"), "Attic");
  await userEvent.type(
    modal.getByLabelText("Server URL"),
    "https://jellyfin.example",
  );
  await userEvent.type(modal.getByLabelText("API Key"), "0007");
  await userEvent.click(
    modal.getByRole("switch", { name: "Refresh episodes" }),
  );
  await userEvent.click(modal.getByRole("radio", { name: "Async" }));
  await userEvent.click(modal.getByRole("button", { name: "Save instance" }));
  await waitFor(() => expect(writes).toHaveLength(1));
  expect(writes[0]).toMatchObject({
    kind: "jellyfin",
    name: "Attic",
    url: "https://jellyfin.example",
    api_key: "0007",
    path_mappings: [],
    refresh_movies: true,
    refresh_episodes: false,
    options: { refresh_method: "async" },
  });
});

it("keeps a library handle the server did not list rather than dropping it", async () => {
  // A library the probe could not reach is not a library the user un-chose.
  setup("plex", [
    instance("plex", { options: { movie_libraries: ["Offline films"] } }),
  ]);
  server.use(
    http.post(`${item}/libraries`, () =>
      HttpResponse.json({ data: [], error_code: null }),
    ),
  );
  await userEvent.click(await screen.findByRole("button", { name: "Edit" }));
  const modal = within(await screen.findByRole("dialog"));
  expect(await modal.findByText("Offline films")).toBeInTheDocument();
});

it.each(["jellyfin", "plex"] as const)(
  "%s can be asked to rescan its libraries without anything being queued",
  async (kind) => {
    // Retry only drains queued targets, so an idle instance had no way to ask
    // the server to re-read itself at all.
    const asked: string[] = [];
    setup(kind);
    server.use(
      http.post(`${item}/refresh-libraries`, ({ request }) => {
        asked.push(request.url);
        return HttpResponse.json({ requested: 3 });
      }),
    );
    await userEvent.click(
      await screen.findByRole("button", { name: "Refresh libraries" }),
    );
    expect(
      await screen.findByText(/Asked the server to rescan 3 libraries/),
    ).toBeInTheDocument();
    expect(asked).toHaveLength(1);
  },
);

it("says how many libraries the server refused to rescan", async () => {
  // One refused scope used to abort the run; now the rest are still asked and
  // the refusal is reported beside them.
  setup("plex");
  server.use(
    http.post(`${item}/refresh-libraries`, () =>
      HttpResponse.json({ requested: 2, failed: 1 }),
    ),
  );
  await userEvent.click(
    await screen.findByRole("button", { name: "Refresh libraries" }),
  );
  expect(
    await screen.findByText(
      "Asked the server to rescan 2 libraries. 1 could not be rescanned. The log names it.",
    ),
  ).toBeInTheDocument();
});

it("names a blocked one-time import instead of blaming the connection", async () => {
  setup("jellyfin");
  server.use(
    http.get("/api/system/media-server-instances/:id/status", () =>
      HttpResponse.json({
        pending: 0,
        state: "unconfirmed",
        error_code: "migration_failed",
      }),
    ),
  );
  expect(
    await screen.findByText(/one-time import of this kind's old settings/),
  ).toBeInTheDocument();
});

it.each(["jellyfin", "plex"] as const)(
  "tells a %s user to choose a library rather than to check path mappings",
  async (kind) => {
    setup(kind);
    server.use(
      http.get("/api/system/media-server-instances/:id/status", () =>
        HttpResponse.json({
          pending: 1,
          state: "unconfirmed",
          error_code: "library_missing",
        }),
      ),
    );
    expect(
      await screen.findByText(/no library selected for that kind of media/),
    ).toBeInTheDocument();
    // Path mappings are not something either kind has to check.
    expect(screen.queryByText(/path mappings/)).not.toBeInTheDocument();
  },
);
