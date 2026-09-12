import { createMemoryRouter, RouterProvider } from "react-router";
import { expect, it } from "vitest";
import { AllProviders } from "@/providers";
import { rawRender, screen } from "@/tests";
import Discover from "./testHarness";

it("offers global movie browsing without requiring a library or subtitle language", async () => {
  const { user } = browse();
  expect(screen.getByLabelText("Search")).toBeEnabled();
  // The homepage carries no retrieval controls. Language and Find live in
  // detail once a title is selected.
  expect(screen.queryByLabelText("Subtitle language")).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Find subtitles" }),
  ).not.toBeInTheDocument();
  expect(screen.queryByLabelText("IMDb ID")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Shōgun" }),
  ).toBeInTheDocument();
  expect(selectInput("Subtitle language")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
});

/* eslint-disable camelcase -- API fixture fields. */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { DiscoverSetupReturn } from "@/contexts/Discover";
import DiscoverSettings from "@/pages/Settings/Discover";
import { waitFor } from "@/tests";
import server from "@/tests/mocks/node";

const movie = {
  source: "tmdb",
  source_id: "tmdb:movie:42",
  id: 42,
  media_type: "movie",
  title: "Shōgun",
  year: 1980,
  imdb_id: "tt0080274",
  mapping_status: "resolved",
  overview: "A film outside the library.",
  poster_url: null,
  backdrop_url: null,
};
const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "metadata-one",
  locale: "en-US",
  message: "TMDB is available.",
  checked_at: "2026-09-08T10:00:00Z",
  fetched_at: "2026-09-08T10:00:00Z",
};

function browse(initial = "/discover") {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      {
        path: "/subtitle-hub",
        element: (
          <DiscoverSetupReturn>
            <DiscoverSettings />
          </DiscoverSetupReturn>
        ),
      },
    ],
    { initialEntries: [initial] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}

beforeEach(() => {
  localStorage.clear();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "metadata-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code2: "en", code3: "eng", enabled: false },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [movie] } }),
    ),
    http.get("/api/discover/metadata/movies/42", () =>
      HttpResponse.json({ data: { ...envelope, item: movie } }),
    ),
  );
});

it("debounces title queries, opens mapped movie details, and submits only after explicit language and Find subtitles", async () => {
  const lookups: string[] = [];
  const providers: unknown[] = [];
  server.use(
    http.get("/api/discover/metadata/search", ({ request }) => {
      lookups.push(new URL(request.url).searchParams.get("q") ?? "");
      return HttpResponse.json({ data: { ...envelope, items: [movie] } });
    }),
    http.post("/api/discover/search", async ({ request }) => {
      providers.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "...Shōgun!!!");
  expect(lookups).toEqual([]);
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Shōgun" }),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274");
  expect(lookups).toEqual(["shogun", "shogun"]);
  expect(providers).toEqual([]);
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(providers).toHaveLength(1));
  expect(providers[0]).toEqual({
    media_type: "movie",
    imdb_id: "tt0080274",
    title: "Shōgun",
    year: 1980,
    language: "eng",
    refresh: false,
  });
});

it("closes suggestions once on Escape and never reuses matches for punctuation-only input", async () => {
  const { user, router } = browse();
  const query = screen.getByLabelText("Search");
  await user.type(query, "Shogun");
  await screen.findByRole("button", { name: "Shōgun (1980)" });
  await user.keyboard("{Escape}{Escape}");
  expect(
    screen.queryByRole("button", { name: "Shōgun (1980)" }),
  ).not.toBeInTheDocument();
  expect(router.state.location.pathname).toBe("/discover");
  await user.clear(query);
  await user.type(query, "...!!!{Enter}");
  expect(
    screen.queryByRole("button", { name: "Shōgun (1980)" }),
  ).not.toBeInTheDocument();
  // The homepage carries no retrieval controls, so punctuation-only input
  // leaves no IMDb field behind rather than an empty one.
  expect(screen.queryByLabelText("IMDb ID")).not.toBeInTheDocument();
});

it("ignores an obsolete lookup response after the query changes", async () => {
  let release: (() => void) | undefined;
  server.use(
    http.get("/api/discover/metadata/search", async ({ request }) => {
      if (new URL(request.url).searchParams.get("q") === "old") {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json({
          data: { ...envelope, items: [{ ...movie, title: "Obsolete film" }] },
        });
      }
      return HttpResponse.json({ data: { ...envelope, items: [movie] } });
    }),
  );
  const { user } = browse();
  const query = screen.getByLabelText("Search");
  await user.type(query, "old");
  await waitFor(() => expect(release).toBeDefined());
  await user.clear(query);
  await user.type(query, "Shogun");
  await screen.findByRole("button", { name: "Shōgun (1980)" });
  release?.();
  await queryClient.refetchQueries({ type: "active" });
  expect(
    screen.queryByRole("button", { name: /Obsolete film/ }),
  ).not.toBeInTheDocument();
});

it("returns from movie details to the query, browsing position and visible candidate focus", async () => {
  const scrolling = vi.spyOn(window, "scrollTo");
  const { user } = browse("/discover?view=movies#browsing");
  await user.type(screen.getByLabelText("Search"), "Shogun");
  const button = await screen.findByRole("button", { name: "Shōgun (1980)" });
  await user.click(button);
  await screen.findByRole("heading", { name: "Shōgun" });
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await user.click(screen.getByLabelText("Search"));
  expect(screen.getByLabelText("Search")).toHaveValue("Shogun");
  await waitFor(() => expect(screen.getByLabelText("Search")).toHaveFocus());
  expect(scrolling).toHaveBeenCalled();
  scrolling.mockRestore();
});

it("explains unresolved IMDb mapping without keeping an earlier searchable identity", async () => {
  server.use(
    http.get("/api/discover/metadata/movies/42", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          item: { ...movie, imdb_id: null, mapping_status: "unresolved" },
        },
      }),
    ),
  );
  const { user } = browse();
  // The homepage carries no retrieval controls, so there is no earlier
  // homepage identity to keep. Select the unresolved title, then confirm the
  // detail clears the identity and stays unsearchable even with a language.
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  expect(
    await screen.findByText(/TMDB has no resolved IMDb identity/),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("");
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
});

it("keeps explicit IMDb retrieval usable without metadata setup", async () => {
  // Without TMDB the global catalog is unavailable, but a local candidate
  // still carries a usable title identity. The explicit value below arrives
  // sourced from the local record rather than typed on the homepage, which
  // no longer carries retrieval controls.
  const local = {
    ...movie,
    source: "local",
    source_id: "local:movie:42",
    imdb_id: "tt0133093",
    title: "Shōgun",
  };
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: false,
          metadata_revision: "metadata-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [local] } }),
    ),
    http.get("/api/discover/metadata/movies/42", ({ request }) =>
      HttpResponse.json({
        data: {
          ...envelope,
          source: new URL(request.url).searchParams.get("source") ?? "local",
          item: local,
        },
      }),
    ),
  );
  const { user } = browse();
  await user.click(screen.getByLabelText("Search"));
  await user.type(screen.getByLabelText("Search"), "S");
  await screen.findByText(/Set up TMDB in the Subtitle Hub/);
  await user.clear(screen.getByLabelText("Search"));
  expect(screen.queryByLabelText("IMDb ID")).not.toBeInTheDocument();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093"),
  );
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeEnabled();
});

it("retires old metadata on token rotation even while configured remains true", async () => {
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await screen.findByRole("button", { name: "Shōgun (1980)" });
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "metadata-two",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: { ...envelope, revision: "metadata-two" } }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({
        data: { ...envelope, revision: "metadata-two", items: [] },
      }),
    ),
  );
  await queryClient.refetchQueries({ queryKey: [QueryKeys.System] });
  await waitFor(() =>
    expect(
      screen.queryByRole("button", { name: "Shōgun (1980)" }),
    ).not.toBeInTheDocument(),
  );
  expect(
    await screen.findByText(/No catalog titles matched/),
  ).toBeInTheDocument();
});

it("preserves the selected subtitle identity when metadata credentials are removed", async () => {
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await screen.findByRole("heading", { name: "Shōgun" });
  await pickOption(user, "Subtitle language", "English");
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274");
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: false,
          metadata_revision: "removed",
          locale: "en-US",
        },
      }),
    ),
  );
  await queryClient.refetchQueries({ queryKey: [QueryKeys.System] });
  await screen.findByText(/Set up TMDB in the Subtitle Hub to load/);
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeEnabled();
});

it.each(["IMDb ID", "Media type"])(
  "retires sourced movie details and title when the manual %s changes",
  async (field) => {
    const submitted: unknown[] = [];
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        submitted.push(await request.json());
        return new HttpResponse(null, { status: 503 });
      }),
    );
    const { user } = browse();
    await user.type(screen.getByLabelText("Search"), "Shogun");
    await user.click(
      await screen.findByRole("button", { name: "Shōgun (1980)" }),
    );
    await screen.findByRole("heading", { name: "Shōgun" });
    await pickOption(user, "Subtitle language", "English");
    if (field === "IMDb ID") {
      await openSearchOptions(user);
      await user.clear(screen.getByLabelText(field));
    } else {
      await chooseSegment(user, "Episode");
    }
    expect(
      screen.queryByRole("heading", { name: "Shōgun" }),
    ).not.toBeInTheDocument();
    expect(screen.getByLabelText("Search")).toBeEnabled();
    expect(submitted).toEqual([]);
    if (field === "IMDb ID") {
      await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
    } else {
      await user.type(screen.getByRole("textbox", { name: "Season" }), "1");
      await user.type(screen.getByRole("textbox", { name: "Episode" }), "2");
      await user.click(
        screen.getByRole("checkbox", { name: /I confirm this series IMDb ID/ }),
      );
    }
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(submitted).toHaveLength(1));
    expect(submitted[0]).toMatchObject({
      media_type: field === "IMDb ID" ? "movie" : "episode",
      imdb_id: field === "IMDb ID" ? "tt0133093" : "tt0080274",
      language: "eng",
    });
    expect(submitted[0]).not.toHaveProperty("title");
  },
);

it.each([
  "same",
  "different",
  "unresolved",
  "fresh unchanged",
  "fresh changed",
  "fresh unresolved",
])(
  "keeps accepted subtitle results only when reopening the %s source movie",
  async (selection) => {
    let searches = 0;
    let detailsRequests = 0;
    let releaseDetails: (() => void) | undefined;
    const submitted: Record<string, unknown>[] = [];
    const fresh = selection.startsWith("fresh ");
    const freshMovie = {
      ...movie,
      imdb_id:
        selection === "fresh changed"
          ? "tt0133093"
          : selection === "fresh unresolved"
            ? null
            : movie.imdb_id,
      mapping_status:
        selection === "fresh unresolved" ? "unresolved" : "resolved",
    };
    const candidates = [
      movie,
      {
        ...movie,
        id: 43,
        source_id: "tmdb:movie:43",
        title: "Different film",
        imdb_id: "tt0133093",
      },
      {
        ...movie,
        id: 44,
        source_id: "tmdb:movie:44",
        title: "Unresolved film",
        imdb_id: null,
        mapping_status: "unresolved",
      },
    ];
    server.use(
      http.get("/api/discover/metadata/search", () =>
        HttpResponse.json({ data: { ...envelope, items: candidates } }),
      ),
      http.get("/api/discover/metadata/movies/:id", async ({ params }) => {
        detailsRequests += 1;
        if (fresh && detailsRequests === 2) {
          await new Promise<void>((resolve) => {
            releaseDetails = resolve;
          });
        }
        return HttpResponse.json({
          data: {
            ...envelope,
            item:
              fresh && detailsRequests === 2
                ? freshMovie
                : candidates.find(
                    (candidate) => String(candidate.id) === params.id,
                  ),
          },
        });
      }),
      http.post("/api/discover/search", async ({ request }) => {
        searches += 1;
        const { refresh, ...context } = (await request.json()) as Record<
          string,
          unknown
        >;
        expect(refresh).toBe(false);
        submitted.push(context);
        return HttpResponse.json({
          search_id: "accepted-movie-search",
          context: { ...context, matching_mode: "title" },
          status: "complete",
          checked_at: envelope.checked_at,
          attempted_at: envelope.checked_at,
          cache_status: "fresh",
          coverage: {
            complete: true,
            configured_count: 1,
            completed_count: 1,
            providers: [],
          },
          results: [
            {
              id: "accepted-row",
              search_id: "accepted-movie-search",
              provider: "fixture-provider",
              language: "en",
              language_variant: null,
              release: "Shogun.1980.Web",
              scope: "full",
              hearing_impaired: false,
              matches: ["imdb_id"],
              compatibility_score: 10,
              compatibility_score_max: 119,
              rating: null,
              uploader: null,
              checked_at: envelope.checked_at,
              expires_at: new Date(Date.now() + 3600000).toISOString(),
              stale: false,
            },
          ],
        });
      }),
      http.get(
        "/api/discover/download",
        () => new HttpResponse(null, { status: 410 }),
      ),
    );
    const { user } = browse();
    await user.type(screen.getByLabelText("Search"), "Shogun");
    await user.click(
      await screen.findByRole("button", { name: "Shōgun (1980)" }),
    );
    await screen.findByRole("heading", { name: "Shōgun" });
    await pickOption(user, "Subtitle language", "English");
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByRole("heading", {
      name: "Shogun.1980.Web",
    });
    // The title hero is an article too; the result row is the one that
    // offers the download.
    await user.click(screen.getByRole("button", { name: "Download SRT" }));
    await screen.findByText(/This result has expired/);
    await user.click(screen.getByRole("button", { name: "Back to Discover" }));
    await user.click(screen.getByLabelText("Search"));
    // The homepage carries no retrieval form or results. The accepted snapshot
    // stays in context, but its heading and expired notice are hidden until
    // the same title is selected again.
    expect(
      screen.queryByRole("heading", { name: "Shogun.1980.Web" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(/This result has expired/),
    ).not.toBeInTheDocument();
    if (fresh) {
      queryClient.removeQueries({
        queryKey: [QueryKeys.Discover, "metadata", "metadata-one", "movies/42"],
        type: "inactive",
      });
    }
    const selected = fresh
      ? freshMovie
      : candidates[
          selection === "same" ? 0 : selection === "different" ? 1 : 2
        ];
    await user.click(
      await screen.findByRole("button", { name: `${selected.title} (1980)` }),
    );
    if (fresh) {
      await waitFor(() => expect(releaseDetails).toBeDefined());
      expect(screen.getByText("Loading movie details.")).toBeInTheDocument();
      releaseDetails?.();
      await waitFor(() =>
        expect(
          screen.queryByText("Loading movie details."),
        ).not.toBeInTheDocument(),
      );
      expect(detailsRequests).toBe(2);
    }
    await screen.findByRole("heading", { name: selected.title });
    expect(searches).toBe(1);
    expect(screen.getByLabelText("IMDb ID")).toHaveValue(
      selected.imdb_id ?? "",
    );
    if (selection === "same" || selection === "fresh unchanged") {
      expect(
        screen.getByRole("heading", { name: "Shogun.1980.Web" }),
      ).toBeInTheDocument();
      expect(screen.getByText(/This result has expired/)).toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Download SRT" }),
      ).toBeDisabled();
    } else {
      expect(
        screen.queryByRole("heading", { name: "Shogun.1980.Web" }),
      ).not.toBeInTheDocument();
      expect(
        screen.queryByText(/This result has expired/),
      ).not.toBeInTheDocument();
      if (selection === "unresolved" || selection === "fresh unresolved")
        expect(
          screen.getByRole("button", { name: "Find subtitles" }),
        ).toBeDisabled();
      if (selection === "fresh changed") {
        await user.click(
          screen.getByRole("button", { name: "Find subtitles" }),
        );
        await waitFor(() => expect(submitted).toHaveLength(2));
        expect(submitted[1]).toMatchObject({
          imdb_id: "tt0133093",
          media_type: "movie",
          title: movie.title,
        });
      }
    }
  },
);

it("keeps movie and show numeric identities separate across feed filter changes", async () => {
  const show = {
    ...movie,
    source_id: "tmdb:show:42",
    media_type: "show",
    title: "Shōgun",
    year: 2024,
    imdb_id: "tt2788316",
    tvdb_id: 392573,
    seasons: [],
  };
  server.use(
    http.get("/api/discover/metadata/search", ({ request }) =>
      HttpResponse.json({
        data: {
          ...envelope,
          items: [
            new URL(request.url).searchParams.get("type") === "show"
              ? show
              : movie,
          ],
        },
      }),
    ),
    http.get("/api/discover/metadata/shows/42", () =>
      HttpResponse.json({ data: { ...envelope, item: show } }),
    ),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await user.click(screen.getByLabelText("Search"));
  // A feed filter does not change universal search. The same query still
  // offers both media types, identified independently despite the shared id.
  await user.click(await screen.findByRole("button", { name: "Series" }));
  await user.click(screen.getByLabelText("Search"));
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (2024)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt2788316"),
  );
  await openSearchOptions(user);
  expect(screen.getByRole("radio", { name: "Episode" })).toBeChecked();
  expect(screen.getByRole("textbox", { name: "Episode" })).toHaveValue("");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
});

it("browses OMDB with TMDB absent and retains the source through explicit title-only submission", async () => {
  const fallback = {
    ...movie,
    source: "omdb",
    source_id: "omdb:movie:tt0080274",
    id: "tt0080274",
  };
  const sources: (string | null)[] = [];
  const submissions: unknown[] = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: false,
          metadata_revision: "metadata-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          status: "unconfigured",
          configured: false,
          fallback_revision: "omdb-one",
        },
      }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          source: "omdb",
          primary: {
            status: "unconfigured",
            message: "Set up TMDB for the primary catalog.",
          },
          items: [fallback],
        },
      }),
    ),
    http.get("/api/discover/metadata/movies/tt0080274", ({ request }) => {
      sources.push(new URL(request.url).searchParams.get("source"));
      return HttpResponse.json({
        data: { ...envelope, source: "omdb", item: fallback },
      });
    }),
    http.post("/api/discover/search", async ({ request }) => {
      submissions.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  expect(sources).toEqual(["omdb"]);
  expect(submissions).toEqual([]);
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() =>
    expect(submissions).toEqual([
      {
        media_type: "movie",
        imdb_id: "tt0080274",
        title: "Shōgun",
        year: 1980,
        language: "eng",
        refresh: false,
      },
    ]),
  );
});

it("keeps numeric local and TMDB movie identities separate and submits no copy fields", async () => {
  const local = {
    ...movie,
    source: "local",
    source_id: "local:movie:42",
    title: "Local edition",
    imdb_id: "tt0133093",
    copies: [
      {
        local_id: 42,
        arr_instance_id: 2,
        updated_at: null,
        episode_count: null,
      },
    ],
  };
  const requested: (string | null)[] = [];
  const submitted: unknown[] = [];
  server.use(
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [movie, local] } }),
    ),
    http.get("/api/discover/metadata/movies/42", ({ request }) => {
      const source = new URL(request.url).searchParams.get("source");
      requested.push(source);
      return HttpResponse.json({
        data: { ...envelope, source, item: source === "local" ? local : movie },
      });
    }),
    http.post("/api/discover/search", async ({ request }) => {
      submitted.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await user.click(screen.getByLabelText("Search"));
  await user.click(
    await screen.findByRole("button", { name: "Local edition (1980)" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093"),
  );
  expect(requested).toEqual(["tmdb", "local"]);
  expect(screen.getByText("In your library")).toBeVisible();
  expect(screen.queryByLabelText("Library copy")).toBeNull();
  expect(submitted).toEqual([]);
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() =>
    expect(submitted).toEqual([
      {
        media_type: "movie",
        imdb_id: "tt0133093",
        title: "Local edition",
        year: 1980,
        language: "eng",
        refresh: false,
      },
    ]),
  );
});

it("shows scoped local episode counts and requires explicit manual confirmation without TMDB episode routes", async () => {
  const show = {
    ...movie,
    source: "local",
    source_id: "local:show:42",
    media_type: "show",
    title: "Local show",
    tvdb_id: 300,
    seasons: null,
    copies: [
      { local_id: 42, arr_instance_id: 2, updated_at: null, episode_count: 3 },
    ],
    ownership: {
      episode_count: 3,
      unknown_owners: false,
      truncated: false,
      selected_episode_owned: null,
      complete_series: null,
    },
  };
  const requests: string[] = [];
  const submitted: unknown[] = [];
  server.use(
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [show] } }),
    ),
    http.get("/api/discover/metadata/shows/*", ({ request }) => {
      requests.push(request.url);
      return HttpResponse.json({
        data: { ...envelope, source: "local", item: show },
      });
    }),
    http.post("/api/discover/search", async ({ request }) => {
      submitted.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
  const { user } = browse();
  // The search mock answers with the local show whatever the scope, so no
  // tab switch is needed to reach it here.
  await user.type(screen.getByLabelText("Search"), "Local");
  await user.click(
    await screen.findByRole("button", { name: "Local show (1980)" }),
  );
  await screen.findByText("In your library");
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274");
  expect(
    screen.queryByRole("combobox", { name: "Season" }),
  ).not.toBeInTheDocument();
  expect(requests).toHaveLength(1);
  expect(new URL(requests[0]).searchParams.get("source")).toBe("local");
  await pickOption(user, "Subtitle language", "English");
  await user.type(screen.getByRole("textbox", { name: "Season" }), "2");
  await user.type(screen.getByRole("textbox", { name: "Episode" }), "7");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  await user.click(
    screen.getByLabelText(
      "I confirm this series IMDb ID and the manual season and episode numbers",
    ),
  );
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() =>
    expect(submitted).toEqual([
      {
        media_type: "episode",
        imdb_id: "tt0080274",
        title: "Local show",
        year: 1980,
        season: 2,
        episode: 7,
        manual_confirmed: true,
        language: "eng",
        refresh: false,
      },
    ]),
  );
});

import { useSettingsMutation } from "@/apis/hooks/system";
import {
  chooseSegment,
  openSearchOptions,
  pickOption,
  selectInput,
} from "./selectTestHelpers";

function SaveMetadataFixture({
  changes,
  retrying = false,
}: {
  changes: LooseObject;
  retrying?: boolean;
}) {
  const save = useSettingsMutation(retrying);
  return (
    <button onClick={() => save.mutate(changes)}>Save fixture settings</button>
  );
}

it.each([
  {
    name: "locale success",
    changes: { "settings-discover-locale": "hu-HU" },
    failure: false,
    retrying: false,
    omdbRemoved: false,
  },
  {
    name: "region success",
    changes: { "settings-discover-region": "HU" },
    failure: false,
    retrying: false,
    omdbRemoved: false,
  },
  {
    name: "locale persisted but refresh failed",
    changes: { "settings-discover-locale": "hu-HU" },
    failure: true,
    retrying: false,
    omdbRemoved: false,
  },
  {
    name: "refresh retry",
    changes: {},
    failure: true,
    retrying: true,
    omdbRemoved: false,
  },
  {
    name: "mixed save",
    changes: {
      "settings-discover-locale": "hu-HU",
      "settings-omdb-apikey": "synthetic-replacement",
    },
    failure: false,
    retrying: false,
    omdbRemoved: true,
  },
])(
  "invalidates only affected metadata entries for $name through the actual settings hook",
  async ({ changes, failure, retrying, omdbRemoved }) => {
    const key = (source: string) => [
      QueryKeys.Discover,
      "metadata",
      "scope-one",
      "movies/42",
      undefined,
      "movie",
      source,
    ];
    for (const source of ["tmdb", "omdb", "local", "all"])
      queryClient.setQueryData(key(source), { source });
    server.use(
      http.post("/api/system/settings", () =>
        failure
          ? HttpResponse.json(
              { code: "discover_settings_refresh_failed" },
              { status: 503 },
            )
          : HttpResponse.json({}),
      ),
    );
    rawRender(
      <AllProviders>
        <SaveMetadataFixture changes={changes} retrying={retrying} />
      </AllProviders>,
    );
    const user = userEvent.setup();
    await user.click(
      screen.getByRole("button", { name: "Save fixture settings" }),
    );
    await waitFor(() =>
      expect(queryClient.getQueryData(key("tmdb"))).toBeUndefined(),
    );
    expect(queryClient.getQueryData(key("all"))).toBeUndefined();
    expect(queryClient.getQueryData(key("local"))).toEqual({ source: "local" });
    expect(queryClient.getQueryData(key("omdb"))).toEqual(
      omdbRemoved ? undefined : { source: "omdb" },
    );
  },
);

it("forwards the literal local query separately from normalized global metadata search", async () => {
  const queries: URLSearchParams[] = [];
  server.use(
    http.get("/api/discover/metadata/search", ({ request }) => {
      queries.push(new URL(request.url).searchParams);
      return HttpResponse.json({ data: { ...envelope, items: [] } });
    }),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "50%_Done\\Path");
  await waitFor(() => expect(queries).toHaveLength(2));
  expect(queries.map((query) => query.get("type")).sort()).toEqual([
    "movie",
    "show",
  ]);
  for (const query of queries) {
    expect(query.get("q")).toBe("50 done path");
    expect(query.get("local_q")).toBe("50%_Done\\Path");
    expect(query.get("source")).toBe("all");
  }
});

it("cancels a retired TMDB response while allowing an unaffected OMDB response to finish", async () => {
  const key = (source: string) => [
    QueryKeys.Discover,
    "metadata",
    "scope-one",
    "movies/42",
    undefined,
    "movie",
    source,
  ];
  let finishTmdb: (value: unknown) => void = vi.fn();
  let finishOmdb: (value: unknown) => void = vi.fn();
  const tmdb = queryClient
    .fetchQuery({
      queryKey: key("tmdb"),
      queryFn: () =>
        new Promise((resolve) => {
          finishTmdb = resolve;
        }),
    })
    .catch(() => undefined);
  const omdb = queryClient.fetchQuery({
    queryKey: key("omdb"),
    queryFn: () =>
      new Promise((resolve) => {
        finishOmdb = resolve;
      }),
  });
  server.use(http.post("/api/system/settings", () => HttpResponse.json({})));
  rawRender(
    <AllProviders>
      <SaveMetadataFixture changes={{ "settings-discover-locale": "hu-HU" }} />
    </AllProviders>,
  );
  await userEvent
    .setup()
    .click(screen.getByRole("button", { name: "Save fixture settings" }));
  await waitFor(() =>
    expect(queryClient.getQueryState(key("tmdb"))).toBeUndefined(),
  );
  finishTmdb({ title: "Obsolete source" });
  finishOmdb({ title: "Independent source" });
  await Promise.all([tmdb, omdb]);
  expect(queryClient.getQueryData(key("tmdb"))).toBeUndefined();
  expect(queryClient.getQueryData(key("omdb"))).toEqual({
    title: "Independent source",
  });
});

it.each([
  { reason: "quota", cached: false },
  { reason: "quota", cached: true },
  { reason: "timeout", cached: false },
  { reason: "timeout", cached: true },
])(
  "shows the safe OMDB $reason recovery for cached=$cached details without submitting providers",
  async ({ reason, cached }) => {
    const fallback = {
      ...movie,
      source: "omdb",
      source_id: "omdb:movie:tt0080274",
      id: "tt0080274",
    };
    const message =
      reason === "quota"
        ? "OMDB quota is exhausted. Check your allowance before retrying."
        : "OMDB request timed out. Retry metadata.";
    const submissions: unknown[] = [];
    server.use(
      http.get("/api/discover/metadata/status", () =>
        HttpResponse.json({
          data: {
            ...envelope,
            status: "unavailable",
            message: "TMDB is temporarily unavailable.",
            fallback_revision: "fallback-one",
          },
        }),
      ),
      http.get("/api/discover/metadata/search", () =>
        HttpResponse.json({
          data: {
            ...envelope,
            source: "omdb",
            items: [fallback],
            primary: {
              status: "unavailable",
              message: "TMDB is temporarily unavailable.",
            },
          },
        }),
      ),
      http.get("/api/discover/metadata/movies/tt0080274", () =>
        HttpResponse.json({
          data: {
            ...envelope,
            source: "omdb",
            status: cached ? "cached" : "unavailable",
            failure_reason: reason,
            service_status: "unavailable",
            message,
            item: cached ? fallback : null,
          },
        }),
      ),
      http.post("/api/discover/search", async ({ request }) => {
        submissions.push(await request.json());
        return new HttpResponse(null, { status: 503 });
      }),
    );
    const { user } = browse();
    await user.type(screen.getByLabelText("Search"), "Shogun");
    await user.click(
      await screen.findByRole("button", { name: "Shōgun (1980)" }),
    );
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(
      screen.getByText("TMDB is temporarily unavailable."),
    ).toBeInTheDocument();
    expect(submissions).toEqual([]);
    if (cached)
      expect(screen.getByText(/Cached title details/)).toBeInTheDocument();
  },
);

it.each([
  { reason: "quota", cached: false },
  { reason: "quota", cached: true },
  { reason: "timeout", cached: false },
  { reason: "timeout", cached: true },
])(
  "keeps local candidates and primary status visible with $reason and cached=$cached",
  async ({ reason, cached }) => {
    const local = {
      ...movie,
      source: "local",
      source_id: "local:movie:42",
      id: 42,
    };
    const fallback = {
      ...movie,
      source: "omdb",
      source_id: "omdb:movie:tt0080274",
      id: "tt0080274",
      title: "Cached title",
    };
    const message =
      reason === "quota"
        ? "OMDB quota is exhausted. Check your allowance before retrying."
        : "OMDB request timed out. Retry metadata.";
    const submissions: unknown[] = [];
    server.use(
      http.get("/api/discover/metadata/search", () =>
        HttpResponse.json({
          data: {
            ...envelope,
            source: cached ? "omdb" : "tmdb",
            status: cached ? "cached" : "unavailable",
            message: "TMDB is temporarily unavailable.",
            primary: {
              status: "unavailable",
              message: "TMDB is temporarily unavailable.",
            },
            fallback: {
              status: cached ? "cached" : "unavailable",
              failure_reason: reason,
              message,
            },
            items: cached ? [fallback, local] : [local],
          },
        }),
      ),
      http.get("/api/discover/metadata/movies/42", ({ request }) => {
        expect(new URL(request.url).searchParams.get("source")).toBe("local");
        return HttpResponse.json({
          data: { ...envelope, source: "local", item: local },
        });
      }),
      http.post("/api/discover/search", async ({ request }) => {
        submissions.push(await request.json());
        return new HttpResponse(null, { status: 503 });
      }),
    );
    const { user } = browse();
    await user.type(screen.getByLabelText("Search"), "Shogun");
    expect(await screen.findByText(message)).toBeInTheDocument();
    expect(
      screen.getByText("TMDB is temporarily unavailable."),
    ).toBeInTheDocument();
    if (cached)
      expect(
        screen.getByRole("button", { name: "Cached title (1980)" }),
      ).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Shōgun (1980)" }));
    expect(await screen.findByText("In your library")).toBeInTheDocument();
    expect(submissions).toEqual([]);
  },
);

it("heads the title retrieval with its purpose and the remembered-language note", async () => {
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Find the subtitles you need" }),
  ).toBeInTheDocument();
  expect(screen.getByText(/remembered for Discover/)).toBeInTheDocument();
  expect(
    screen.getByText(/Library profiles stay unchanged/),
  ).toBeInTheDocument();
});

it("cancels a pending subtitle search when leaving the title", async () => {
  let release: (() => void) | undefined;
  const requests: unknown[] = [];
  server.use(
    http.post("/api/discover/search", async ({ request }) => {
      requests.push(await request.json());
      await new Promise<void>((resolve) => {
        release = resolve;
      });
      return HttpResponse.json({
        search_id: "search-1",
        context: {
          media_type: "movie",
          imdb_id: "tt0080274",
          language: "eng",
          matching_mode: "title",
        },
        status: "complete",
        checked_at: "2026-09-08T10:00:00Z",
        attempted_at: "2026-09-08T10:00:00Z",
        cache_status: "fresh",
        coverage: {
          complete: true,
          configured_count: 1,
          completed_count: 1,
          providers: [],
        },
        results: [],
      });
    }),
  );
  const { user } = browse();
  await user.type(screen.getByLabelText("Search"), "Shogun");
  await user.click(
    await screen.findByRole("button", { name: "Shōgun (1980)" }),
  );
  await screen.findByRole("heading", { name: "Shōgun" });
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(requests).toHaveLength(1));
  await waitFor(() => expect(release).toBeDefined());
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await user.click(screen.getByLabelText("Search"));
  release?.();
  await waitFor(() =>
    expect(
      queryClient.isMutating({
        mutationKey: [QueryKeys.Discover, QueryKeys.Search],
      }),
    ).toBe(0),
  );
  // The late response belongs to a selection the reader has already left, so
  // nothing is filed. Back lands on the homepage, which carries no retrieval
  // form or results.
  expect(
    screen.queryByRole("heading", { name: /Subtitle results/ }),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/No subtitles matched/)).not.toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Find subtitles" }),
  ).not.toBeInTheDocument();
});
