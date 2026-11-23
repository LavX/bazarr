/* eslint-disable camelcase */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { invalidateSourceDetails } from "@/apis/queries/sourceDetails";
import client from "@/apis/raw/client";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { sourceDetailApi, sourceDetailKeys } from "./api";
import {
  SourceLeagueDetailView,
  SourceMovieDetailView,
  SourceSeriesDetailView,
} from ".";

const movie = {
  id: 7,
  source_id: 3,
  library_id: 11,
  kind: "movie",
  group_id: null,
  title: "Folder movie",
  parsed_title: "Folder movie",
  year: "2026",
  poster: null,
  imdb_id: null,
  profile_id: 1,
  monitored: true,
  identity_state: "identified",
  identity_candidates: [],
  path: "/media/movie.mkv",
  scene_name: null,
  season: null,
  episode: null,
  arr_shadow: false,
  audio_language: ["English", "Japanese"],
  subtitles: [
    ["en:hi", "/media/movie.en.hi.srt", 60],
    ["de", null, null, "pgs"],
  ],
  missing_subtitles: ["hu:forced"],
};

function openPage(kind: "movie" | "series" | "sports") {
  const route = `/${kind === "movie" ? "movies" : kind}/source`;
  const router = createMemoryRouter(
    [
      {
        path: `${route}/:id`,
        element:
          kind === "movie" ? (
            <SourceMovieDetailView />
          ) : kind === "series" ? (
            <SourceSeriesDetailView />
          ) : (
            <SourceLeagueDetailView />
          ),
      },
    ],
    {
      initialEntries: [`${route}/7`],
    },
  );
  return rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
}

beforeEach(() => {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: {}, media_sources: { visible: true } }),
    ),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 3,
            name: "Folder owner",
            kind: "folder",
            enabled: true,
            libraries: [{ id: 11, enabled: true, content_kind: "movie" }],
          },
        ],
      }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/sources/items/7", () => HttpResponse.json(movie)),
  );
});

it("groups source events by season and uses the source item action", async () => {
  const user = userEvent.setup();
  const search = vi.fn();
  const identify = vi.fn();
  const events = [
    {
      ...movie,
      kind: "sports_event",
      id: 60,
      title: "Earlier race",
      season: 2026,
      episode: 2,
      event_date: "2026-02-03",
      part_name: "Qualifying",
    },
    {
      ...movie,
      kind: "sports_event",
      id: 61,
      title: "Later race",
      season: 2026,
      event_date: "2026-04-04",
    },
    { ...movie, kind: "sports_event", id: 62, title: "Friendly" },
  ];
  server.use(
    http.get("/api/sources/groups/7", ({ request }) => {
      expect(new URL(request.url).searchParams.get("kind")).toBe("league");
      return HttpResponse.json({
        ...movie,
        kind: "league",
        title: "Folder league",
      });
    }),
    http.get("/api/sources/groups/7/items", () =>
      HttpResponse.json({ data: events, total: events.length }),
    ),
    http.get("/api/sources/items/60", ({ request }) => {
      expect(new URL(request.url).searchParams.get("kind")).toBe(
        "sports_event",
      );
      return HttpResponse.json(events[0]);
    }),
    http.post("/api/sources/items/60/search", () => {
      search();
      return HttpResponse.json({ job_id: null });
    }),
    http.patch("/api/sources/items/60", async ({ request }) => {
      expect(new URL(request.url).searchParams.get("kind")).toBe(
        "sports_event",
      );
      identify(await request.json());
      return HttpResponse.json(events[0]);
    }),
  );
  openPage("sports");
  expect(await screen.findByText("Season 2026")).toBeVisible();
  expect(screen.getByText("Unknown season")).toBeVisible();
  expect(
    screen
      .getAllByRole("row")
      .filter((row) => /race/i.test(row.textContent ?? ""))
      .map((row) => row.textContent),
  ).toEqual([
    expect.stringContaining("Earlier race"),
    expect.stringContaining("Later race"),
  ]);
  expect(screen.getByText("2026-02-03")).toBeVisible();
  await user.click(screen.getByRole("button", { name: "Earlier race" }));
  const dialog = within(await screen.findByRole("dialog"));
  await user.click(dialog.getByRole("button", { name: /^Search$/ }));
  await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
  expect(dialog.getByRole("button", { name: "Manual Search" })).toBeEnabled();
  expect(dialog.getByRole("button", { name: "Upload" })).toBeEnabled();
  await user.click(dialog.getByRole("button", { name: "Identity fix" }));
  const correction = within(
    await screen.findByRole("dialog", { name: "Identity fix" }),
  );
  await user.clear(correction.getByLabelText("Part"));
  await user.type(correction.getByLabelText("Part"), "Sprint");
  await user.click(correction.getByRole("button", { name: "Save" }));
  await waitFor(() =>
    expect(identify).toHaveBeenCalledWith({
      title: "Earlier race",
      year: 2026,
      event_date: "2026-02-03",
      season: 2026,
      round: 2,
      part_name: "Sprint",
    }),
  );
});

it("keeps manual Sports tools available for an unprofiled, shadowed recording", async () => {
  const user = userEvent.setup();
  const event = {
    ...movie,
    id: 60,
    kind: "sports_event",
    title: "Unprofiled recording",
    identity_state: "unmatched",
    profile_id: null,
    effective_profile_id: null,
    arr_shadow: true,
    shadowed_by: "Race archive",
  };
  server.use(
    http.get("/api/sources/groups/7", () =>
      HttpResponse.json({ ...movie, kind: "league", title: "Folder league" }),
    ),
    http.get("/api/sources/groups/7/items", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
    http.get("/api/sources/items/60", () => HttpResponse.json(event)),
  );
  openPage("sports");
  expect(await screen.findByText("Managed by Race archive")).toBeVisible();
  await user.click(await screen.findByRole("button", { name: event.title }));
  const dialog = within(
    await screen.findByRole("dialog", { name: event.title }),
  );
  expect(dialog.getByRole("button", { name: "Search" })).toBeDisabled();
  expect(dialog.getByRole("button", { name: "Manual Search" })).toBeEnabled();
  expect(dialog.getByRole("button", { name: "Upload" })).toBeEnabled();
  expect(dialog.getByRole("button", { name: "Actions" })).toBeEnabled();
  await user.click(dialog.getByRole("button", { name: "Manual Search" }));
  expect(
    await screen.findByRole("dialog", { name: "Search Subtitles" }),
  ).toBeVisible();
});

it.each(["league", "event list", "event dialog"] as const)(
  "keeps confirmed %s absence through a failed revisit and restores it on success",
  async (removed) => {
    const user = userEvent.setup();
    const league = { ...movie, kind: "league", title: "Cached league" };
    const event = {
      ...movie,
      id: 60,
      group_id: league.id,
      kind: "sports_event",
      title: "Cached race",
      season: 2026,
    };
    const restoredLeague = { ...league, title: "Restored league" };
    const restoredEvent = { ...event, title: "Restored race" };
    let status = 200;
    let restored = false;
    const readDetail = vi.fn();
    function respond(resource: typeof removed, data: object) {
      if (resource === removed) {
        readDetail(status);
        if (status !== 200)
          return HttpResponse.json({ error: "request_failed" }, { status });
      }
      return HttpResponse.json(data);
    }
    server.use(
      http.get("/api/sources/groups/7", () =>
        respond("league", restored ? restoredLeague : league),
      ),
      http.get("/api/sources/groups/7/items", () =>
        respond("event list", {
          data: [restored && removed === "event list" ? restoredEvent : event],
          total: 1,
        }),
      ),
      http.get("/api/sources/items/60", () =>
        respond("event dialog", restored ? restoredEvent : event),
      ),
    );
    const { unmount } = openPage("sports");
    const race = await screen.findByRole("button", { name: event.title });
    if (removed === "event dialog") {
      await user.click(race);
      const dialog = within(
        await screen.findByRole("dialog", { name: event.title }),
      );
      await waitFor(() =>
        expect(
          queryClient.getQueryState(sourceDetailKeys.item(60, "sports_event"))
            ?.status,
        ).toBe("success"),
      );
      expect(
        dialog.getByRole("button", { name: "Manual Search" }),
      ).toBeVisible();
      expect(dialog.getByText("movie.en.hi.srt")).toBeVisible();
    }

    const key =
      removed === "league"
        ? sourceDetailKeys.group(7, "league")
        : removed === "event list"
          ? sourceDetailKeys.leagueEvents(7)
          : sourceDetailKeys.item(60, "sports_event");
    const unavailableMessage = {
      league: "This league is no longer available.",
      "event list": "These events are no longer available.",
      "event dialog": "This event is no longer available.",
    }[removed];
    status = 404;
    await act(async () => {
      await invalidateSourceDetails(queryClient, {
        sourceId: 3,
        libraryId: 11,
      });
    });
    expect(await screen.findByText(unavailableMessage)).toBeVisible();
    readDetail.mockClear();
    await act(async () => {
      await invalidateSourceDetails(queryClient, {
        sourceId: 3,
        libraryId: 12,
      });
    });
    expect(readDetail).not.toHaveBeenCalled();

    status = 503;
    if (removed === "event dialog") {
      await user.keyboard("{Escape}");
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await user.click(
        await screen.findByRole("button", { name: event.title }),
      );
    } else {
      unmount();
      openPage("sports");
    }
    await waitFor(() =>
      expect(queryClient.getQueryState(key)?.error).toMatchObject({
        response: { status: 503 },
      }),
    );
    expect(screen.getByText(unavailableMessage)).toBeVisible();

    if (removed === "event dialog") {
      const dialog = within(
        screen.getByRole("dialog", { name: "Event unavailable" }),
      );
      expect(
        dialog.queryByRole("button", { name: "Change Profile" }),
      ).not.toBeInTheDocument();
      expect(
        dialog.queryByRole("button", { name: "Identity fix" }),
      ).not.toBeInTheDocument();
      expect(dialog.queryByText("movie.en.hi.srt")).not.toBeInTheDocument();
    } else {
      expect(
        screen.queryByRole("button", { name: event.title }),
      ).not.toBeInTheDocument();
      if (removed === "league") {
        expect(
          screen.queryByText(league.title, { exact: true }),
        ).not.toBeInTheDocument();
      } else {
        expect(
          screen.getByRole("heading", { name: league.title }),
        ).toBeVisible();
      }
      expect(screen.getByRole("alert")).toBeVisible();
    }

    restored = true;
    status = 200;
    await act(async () => {
      await queryClient.invalidateQueries({ queryKey: key, exact: true });
    });
    if (removed === "event dialog") {
      const dialog = within(
        await screen.findByRole("dialog", { name: restoredEvent.title }),
      );
      expect(
        dialog.getByRole("button", { name: "Change Profile" }),
      ).toBeVisible();
      expect(
        dialog.getByRole("button", { name: "Identity fix" }),
      ).toBeVisible();
      expect(dialog.getByText("movie.en.hi.srt")).toBeVisible();
    } else if (removed === "league") {
      expect(
        await screen.findByRole("heading", { name: restoredLeague.title }),
      ).toBeVisible();
      expect(
        await screen.findByRole("button", { name: event.title }),
      ).toBeVisible();
    } else {
      expect(
        await screen.findByRole("button", { name: restoredEvent.title }),
      ).toBeVisible();
    }
    expect(screen.queryByText(unavailableMessage)).not.toBeInTheDocument();
  },
);

it("reads the source movie despite a colliding Arr cache and normalizes its stored subtitle rows", async () => {
  queryClient.setQueryData([QueryKeys.Movies, 7], { title: "Wrong Arr movie" });
  queryClient.setQueryData(sourceDetailKeys.item(7, "episode"), {
    ...movie,
    kind: "episode",
    title: "Wrong source episode",
  });
  openPage("movie");
  expect(
    await screen.findByRole("heading", { name: /Folder movie/ }),
  ).toBeVisible();
  expect(screen.queryByText("Wrong Arr movie")).not.toBeInTheDocument();
  expect(screen.queryByText("Wrong source episode")).not.toBeInTheDocument();
  expect(screen.getByText("English", { exact: true })).toBeVisible();
  expect(screen.getByText("Japanese", { exact: true })).toBeVisible();
  expect(screen.getByText("movie.en.hi.srt")).toBeVisible();
  expect(screen.getByText("en:HI")).toBeVisible();
  expect(screen.getByText("hu:Forced")).toBeVisible();
  expect(screen.getByText("pgs")).toBeVisible();
  await waitFor(() =>
    expect(screen.getAllByLabelText("Actions")).toHaveLength(1),
  );
  expect(queryClient.getQueryData(sourceDetailKeys.item(7))).toMatchObject({
    id: 7,
    title: "Folder movie",
  });
});

it("labels untagged source audio tracks while preserving named tracks", async () => {
  server.use(
    http.get("/api/sources/items/7", () =>
      HttpResponse.json({
        ...movie,
        audio_language: ["English", null, "Japanese"],
      }),
    ),
  );

  openPage("movie");

  expect(
    await screen.findByRole("heading", { name: /Folder movie/ }),
  ).toBeVisible();
  expect(screen.getAllByLabelText("Audio Language")).toHaveLength(3);
  expect(
    screen
      .getAllByLabelText("Audio Language")
      .map((badge) => badge.textContent),
  ).toEqual(["English", "Unknown", "Japanese"]);
});

it("sends search and scan to the source item and its owning library", async () => {
  const user = userEvent.setup();
  const search = vi.fn();
  const scan = vi.fn();
  server.use(
    http.post("/api/sources/items/7/search", () => {
      search();
      return HttpResponse.json({ job_id: null });
    }),
    http.post("/api/system/media-sources/3/libraries/11/scan", () => {
      scan();
      return HttpResponse.json({ job_id: null });
    }),
  );
  openPage("movie");
  const searchButton = await screen.findByRole("button", { name: "Search" });
  await waitFor(() => expect(searchButton).toBeEnabled());
  await user.click(searchButton);
  await waitFor(() => expect(search).toHaveBeenCalledTimes(1));
  await user.click(screen.getByRole("button", { name: "Scan Disk" }));
  await waitFor(() => expect(scan).toHaveBeenCalledTimes(1));
});

it("clears the profile with source JSON and preserves a failed identity edit for retry", async () => {
  const user = userEvent.setup();
  const patches: unknown[] = [];
  server.use(
    http.patch("/api/sources/items/7", async ({ request }) => {
      const body = await request.json();
      patches.push(body);
      if (body && typeof body === "object" && "profile_id" in body)
        return HttpResponse.json({ ...movie, profile_id: null });
      return HttpResponse.json(
        {
          error: "conflict",
          message:
            "The candidate list changed. Refresh the title and choose again.",
        },
        { status: 409 },
      );
    }),
  );
  openPage("movie");
  await user.click(
    await screen.findByRole("button", { name: "Change Profile" }),
  );
  await user.click(
    await screen.findByRole("button", { name: "Clear Profile" }),
  );
  await waitFor(() => expect(patches).toContainEqual({ profile_id: null }));
  await user.click(screen.getByRole("button", { name: "Identity fix" }));
  const dialog = await screen.findByRole("dialog", { name: "Identity fix" });
  await user.clear(within(dialog).getByLabelText(/^Title/));
  await user.type(within(dialog).getByLabelText(/^Title/), "Corrected movie");
  await user.click(within(dialog).getByRole("button", { name: "Save" }));
  expect(
    await within(dialog).findByText(
      "The candidate list changed. Refresh the title and choose again.",
    ),
  ).toBeVisible();
  expect(within(dialog).getByLabelText(/^Title/)).toHaveValue(
    "Corrected movie",
  );
  expect(patches).toContainEqual({ title: "Corrected movie", year: 2026 });
});

it("pages episodes by group and opens only source episode actions", async () => {
  const user = userEvent.setup();
  const list = vi.fn();
  server.use(
    http.get("/api/sources/groups/7", () =>
      HttpResponse.json({ ...movie, kind: "show", title: "Folder show" }),
    ),
    http.get("/api/sources/items", ({ request }) => {
      const params = new URL(request.url).searchParams;
      list(Object.fromEntries(params));
      return HttpResponse.json({
        data: [
          {
            ...movie,
            id: 71,
            kind: "episode",
            group_id: 7,
            title: "Folder episode",
            season: 2,
            episode: 3,
          },
        ],
        total: 51,
      });
    }),
    http.get("/api/sources/items/71", () =>
      HttpResponse.json({
        ...movie,
        id: 71,
        kind: "episode",
        group_id: 7,
        title: "Folder episode",
      }),
    ),
  );
  openPage("series");
  await user.click(
    await screen.findByRole("button", { name: "Folder episode" }),
  );
  const dialog = await screen.findByRole("dialog", { name: "Folder episode" });
  expect(
    within(dialog).getByRole("button", { name: "Manual Search" }),
  ).toBeVisible();
  expect(list).toHaveBeenCalledWith({
    group_id: "7",
    kind: "episode",
    start: "0",
    length: "50",
  });
  expect(screen.queryByText("Extract Embedded Tracks")).not.toBeInTheDocument();
});

it("keeps profile writes typed as a number or null", async () => {
  const bodies: unknown[] = [];
  const kinds: (string | null)[] = [];
  server.use(
    http.patch("/api/sources/groups/7", async ({ request }) => {
      bodies.push(await request.json());
      kinds.push(new URL(request.url).searchParams.get("kind"));
      return HttpResponse.json({ ...movie, kind: "show" });
    }),
  );
  await sourceDetailApi.updateProfile("groups", 7, 4);
  await sourceDetailApi.updateProfile("groups", 7, null);
  expect(bodies).toEqual([{ profile_id: 4 }, { profile_id: null }]);
  expect(kinds).toEqual(["show", "show"]);
});

it("uses an inherited profile and backend subtitle details", async () => {
  server.use(
    http.get("/api/sources/items/7", () =>
      HttpResponse.json({
        ...movie,
        profile_id: null,
        effective_profile_id: 4,
        subtitle_details: [
          {
            code2: "fr",
            name: "French",
            hi: false,
            forced: false,
            path: "/media/authoritative.fr.srt",
          },
        ],
        missing_subtitle_details: [
          { code2: "es", name: "Spanish", hi: false, forced: true },
        ],
      }),
    ),
  );
  openPage("movie");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Manual Search" })).toBeEnabled(),
  );
  expect(screen.getByText("authoritative.fr.srt")).toBeVisible();
  expect(screen.queryByText("movie.en.hi.srt")).not.toBeInTheDocument();
  expect(screen.getByText("es:Forced")).toBeVisible();
});

it.each(["source", "library"] as const)(
  "disables file actions for a disabled %s while keeping metadata edits available",
  async (disabled) => {
    server.use(
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 3,
              name: "Disabled owner",
              kind: "folder",
              enabled: disabled !== "source",
              libraries: [
                {
                  id: 11,
                  enabled: disabled !== "library",
                  content_kind: "movie",
                },
              ],
            },
          ],
        }),
      ),
    );
    openPage("movie");
    await screen.findByRole("heading", { name: /Folder movie/ });
    await screen.findByText("Disabled owner");
    for (const name of ["Search", "Manual Search", "Upload", "Scan Disk"])
      expect(screen.getByRole("button", { name })).toBeDisabled();
    expect(
      screen.getByRole("button", { name: "Change Profile" }),
    ).toBeEnabled();
    expect(screen.getByRole("button", { name: "Identity fix" })).toBeEnabled();
    expect(screen.queryByLabelText("Actions")).not.toBeInTheDocument();
  },
);

it("allows a group search to select eligible children without a group profile", async () => {
  const user = userEvent.setup();
  const searches = vi.fn();
  server.use(
    http.get("/api/sources/groups/7", () =>
      HttpResponse.json({
        ...movie,
        kind: "show",
        title: "Folder show",
        profile_id: null,
        effective_profile_id: null,
        identity_state: "unmatched",
      }),
    ),
    http.get("/api/sources/items", () =>
      HttpResponse.json({ data: [], total: 0 }),
    ),
    http.post("/api/sources/groups/7/search", () => {
      searches();
      return HttpResponse.json({ job_id: null });
    }),
  );
  openPage("series");
  const search = await screen.findByRole("button", { name: "Search" });
  await waitFor(() => expect(search).toBeEnabled());
  await user.click(search);
  await waitFor(() => expect(searches).toHaveBeenCalledTimes(1));
});

it("deletes only the selected source sidecar and offers no unsupported tools", async () => {
  const user = userEvent.setup();
  const deleted: unknown[] = [];
  server.use(
    http.delete("/api/sources/items/7/subtitles", async ({ request }) => {
      deleted.push(Object.fromEntries(await request.formData()));
      return new HttpResponse(null, { status: 204 });
    }),
  );
  openPage("movie");
  await user.click(await screen.findByLabelText("Actions"));
  expect(
    screen.queryByRole("menuitem", { name: /Sync|OCR|Extract|Hide/ }),
  ).not.toBeInTheDocument();
  await user.click(await screen.findByRole("menuitem", { name: "Delete" }));
  const dialog = await screen.findByRole("dialog", { name: "Delete Subtitle" });
  await user.click(within(dialog).getByRole("button", { name: "Delete" }));
  await waitFor(() =>
    expect(deleted).toEqual([
      {
        subtitles_path: "/media/movie.en.hi.srt",
        language: "en",
        hi: "true",
        forced: "false",
      },
    ]),
  );
});

it("searches and downloads from the source manual routes", async () => {
  const user = userEvent.setup();
  const searches = vi.fn();
  const downloads: unknown[] = [];
  queryClient.setQueryData(
    [QueryKeys.System, QueryKeys.Jobs],
    [{ job_id: 92, status: "completed" }],
  );
  server.use(
    http.post("/api/sources/items/7/manual/search", () => {
      searches();
      return HttpResponse.json({ job_id: 92 });
    }),
    http.get("/api/system/jobs/result", () =>
      HttpResponse.json({
        result: {
          data: [
            {
              score: 99,
              language: "en",
              hearing_impaired: "False",
              forced: "False",
              provider: "test-provider",
              subtitle: "signed-source-result",
              release_info: ["Source release"],
              matches: [],
              dont_matches: [],
            },
          ],
          providers: [],
        },
      }),
    ),
    http.post("/api/sources/items/7/manual", async ({ request }) => {
      downloads.push(Object.fromEntries(await request.formData()));
      return HttpResponse.json({ job_id: null });
    }),
  );
  openPage("movie");
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Manual Search" })).toBeEnabled(),
  );
  await user.click(screen.getByRole("button", { name: "Manual Search" }));
  const dialog = await screen.findByRole("dialog", {
    name: "Search Subtitles",
  });
  await user.click(within(dialog).getByRole("button", { name: "Search" }));
  await user.click(await within(dialog).findByLabelText("Download"));
  await waitFor(() => expect(downloads).toHaveLength(1));
  expect(downloads[0]).toMatchObject({
    provider: "test-provider",
    subtitle: "signed-source-result",
    hi: "False",
    forced: "False",
  });
  expect(searches).toHaveBeenCalledTimes(1);
});

it("uploads a subtitle to its source item with the selected language flags", async () => {
  const post = vi
    .spyOn(client.axios, "post")
    .mockResolvedValueOnce({ data: { job_id: null } });
  try {
    await sourceDetailApi.upload(
      7,
      new File(["1\n00:00:00,000 --> 00:00:01,000\nTest"], "movie.en.srt"),
      "en",
      true,
      false,
    );
    expect(post).toHaveBeenCalledTimes(1);
    const [path, form, config] = post.mock.calls[0];
    expect(path).toBe("/sources/items/7/subtitles");
    expect(config?.headers).toEqual({ "Content-Type": "multipart/form-data" });
    expect(form).toBeInstanceOf(FormData);
    if (!(form instanceof FormData))
      throw new Error("Expected a multipart upload");
    expect(form.get("file")).toMatchObject({ name: "movie.en.srt" });
    expect(form.get("language")).toBe("en");
    expect(form.get("hi")).toBe("true");
    expect(form.get("forced")).toBe("false");
  } finally {
    post.mockRestore();
  }
});
