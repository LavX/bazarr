/* eslint-disable camelcase -- API fixtures retain transport field names. */
/// <reference types="node" />
import {
  createMemoryRouter,
  Link,
  RouterProvider,
  useNavigate,
} from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { readFileSync } from "node:fs";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { act, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { readableTime } from "./feedText";
import {
  findSelectInput,
  openSearchOptions,
  pickOption,
  selectInput,
} from "./selectTestHelpers";
import Discover from "./testHarness";

const discoverStyles = readFileSync(
  "src/pages/Discover/Discover.module.scss",
  "utf8",
);

/**
 * The coarse-pointer block that carries the page's touch floor.
 *
 * jsdom applies no media query and computes no layout, so the stylesheet is
 * what can be asserted. The floor has to sit inside a coarse-pointer block:
 * that is what keeps the fine-pointer sizes the page was tuned for, which is
 * the whole reason the desktop rules are left where they are.
 */
function coarseTouchBlock(source: string): string {
  const marker = "@media (pointer: coarse)";
  for (
    let at = source.indexOf(marker);
    at >= 0;
    at = source.indexOf(marker, at + 1)
  ) {
    const open = source.indexOf("{", at);
    if (open < 0) return "";
    let depth = 0;
    for (let i = open; i < source.length; i += 1) {
      if (source[i] === "{") depth += 1;
      else if (source[i] === "}") {
        depth -= 1;
        if (depth === 0) {
          const block = source.slice(at, i + 1);
          if (block.includes(".wantedLanguages button")) return block;
          break;
        }
      }
    }
  }
  return "";
}

function ThemeToggle() {
  const { toggleColorScheme } = useMantineColorScheme();
  return <Button onClick={() => toggleColorScheme()}>Change appearance</Button>;
}

function renderDiscover() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <>
            <ThemeToggle />
            <Discover />
          </>
        ),
      },
      {
        path: "/subtitle-hub",
        element: <Link to="/discover">Return to Discover</Link>,
      },
      {
        // A route outside Discover, reachable from the shared navigation, for
        // the cases where the reader leaves the whole route.
        path: "/history/series",
        element: <div>Local history page</div>,
      },
    ],
    { initialEntries: ["/discover"] },
  );
  const view = rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup(), router, view };
}

const target = {
  media_type: "movie",
  imdb_id: "tt0133093",
  language: "eng",
  matching_mode: "title",
};

const movie = {
  source: "tmdb",
  source_id: "tmdb:movie:11",
  id: 11,
  media_type: "movie",
  title: "The Matrix",
  year: 1999,
  imdb_id: "tt0133093",
  mapping_status: "resolved",
  overview: "A computer hacker learns about the true nature of reality.",
  poster_url: null,
  backdrop_url: null,
};

const show = {
  source: "tmdb",
  source_id: "tmdb:show:100",
  id: 100,
  media_type: "show",
  title: "Northern Light",
  year: 2020,
  imdb_id: "tt1234567",
  tvdb_id: 300,
  mapping_status: "resolved",
  overview: "A non-library show.",
  poster_url: null,
  backdrop_url: null,
  seasons: [],
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

function snapshot(release = "The.Matrix.1999.1080p") {
  return {
    search_id: "search-1",
    context: target,
    status: "complete",
    checked_at: "2026-09-08T10:00:00Z",
    attempted_at: "2026-09-08T10:00:00Z",
    cache_status: "fresh",
    coverage: {
      complete: true,
      configured_count: 1,
      completed_count: 1,
      providers: [
        {
          provider: "example",
          status: "success",
          result_count: 1,
          reason: null,
          retry_at: null,
          elapsed_ms: 2,
        },
      ],
    },
    results: [
      {
        id: "opaque-1",
        search_id: "search-1",
        provider: "example",
        language: "en",
        language_variant: null,
        release,
        scope: "unknown",
        hearing_impaired: null,
        matches: ["imdb_id"],
        compatibility_score: 10,
        compatibility_score_max: 119,
        rating: null,
        uploader: null,
        checked_at: "2026-09-08T10:00:00Z",
        expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
        stale: false,
      },
    ],
  };
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
        { name: "Hungarian", code2: "hu", code3: "hun", enabled: true },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/metadata/search", ({ request }) => {
      const type = new URL(request.url).searchParams.get("type");
      if (type === "show")
        return HttpResponse.json({ data: { ...envelope, items: [show] } });
      return HttpResponse.json({ data: { ...envelope, items: [movie] } });
    }),
    http.get("/api/discover/metadata/movies/11", () =>
      HttpResponse.json({ data: { ...envelope, item: movie } }),
    ),
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({ data: { ...envelope, item: show } }),
    ),
  );
});

async function selectTitle(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Search"), "Matrix");
  await user.click(
    await screen.findByRole("button", { name: "The Matrix (1999)" }),
  );
  await screen.findByRole("heading", { name: "The Matrix" });
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093"),
  );
}

async function selectShow(user: ReturnType<typeof userEvent.setup>) {
  // The single catalog tab row owns the search scope. Series follows the
  // title search to series titles.
  await user.click(await screen.findByRole("button", { name: "Series" }));
  const query = screen.getByLabelText("Search");
  await user.clear(query);
  await user.type(query, "Northern");
  await user.click(
    await screen.findByRole("button", { name: "Northern Light (2020)" }),
  );
  await screen.findByRole("heading", { name: "Northern Light" });
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt1234567"),
  );
}

async function selectTarget(user: ReturnType<typeof userEvent.setup>) {
  await selectTitle(user);
  await pickOption(user, "Subtitle language", "English");
}

describe("Discover retrieval", () => {
  it.each(["Movies", "Series"])(
    "searches movies and shows while the %s feed is selected",
    async (scope) => {
      const { user } = renderDiscover();
      await user.click(screen.getByRole("button", { name: scope }));
      await user.type(screen.getByLabelText("Search"), "light");
      expect(
        await screen.findByRole("button", { name: "The Matrix (1999)" }),
      ).toBeEnabled();
      expect(
        await screen.findByRole("button", { name: "Northern Light (2020)" }),
      ).toBeEnabled();
      expect(
        screen.queryByRole("button", { name: "Find subtitles" }),
      ).toBeNull();
    },
  );

  it("retains movie candidates while reporting failed show coverage in All media", async () => {
    server.use(
      http.get("/api/discover/metadata/search", ({ request }) => {
        if (new URL(request.url).searchParams.get("type") === "show")
          return new HttpResponse(null, { status: 503 });
        return HttpResponse.json({ data: { ...envelope, items: [movie] } });
      }),
    );
    const { user } = renderDiscover();
    await user.type(screen.getByLabelText("Search"), "light");
    expect(
      await screen.findByRole("button", { name: "The Matrix (1999)" }),
    ).toBeEnabled();
    expect(
      await screen.findByText(/Show metadata could not be loaded/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/No shows matched/)).toBeNull();
  });

  it("keeps IMDb edits in retrieval until returning to Discover", async () => {
    const { user } = renderDiscover();
    await selectTarget(user);
    await openSearchOptions(user);
    await user.clear(screen.getByLabelText("IMDb ID"));
    await user.type(screen.getByLabelText("IMDb ID"), "tt0080274");
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeEnabled();
    expect(screen.getByLabelText("Search")).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Back to Discover" }));
    expect(screen.getByLabelText("Search")).toBeEnabled();
    expect(screen.queryByLabelText("IMDb ID")).toBeNull();
  });

  it("keeps manual episode controls available after changing a movie's media type", async () => {
    const { user } = renderDiscover();
    await selectTarget(user);
    await openSearchOptions(user);
    await user.click(screen.getByRole("radio", { name: "Episode" }));
    expect(screen.getByLabelText("IMDb ID")).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Season" })).toBeEnabled();
    expect(screen.getByRole("textbox", { name: "Episode" })).toBeEnabled();
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    expect(screen.getByLabelText("Search")).toBeEnabled();
  });

  it("keeps the IMDb helper below its input so the manual row shares one input line", async () => {
    const { user } = renderDiscover();
    await selectTarget(user);
    await openSearchOptions(user);
    await user.click(screen.getByRole("radio", { name: "Episode" }));
    const imdb = screen.getByLabelText("IMDb ID");
    // The helper is load-bearing: it is the only thing on the page that says
    // which of the two IMDb IDs the field wants, and it stays the field's
    // accessible description wherever it is drawn.
    expect(imdb).toHaveAccessibleDescription("Use the series IMDb ID.");
    // Mantine renders a description between the label and the input by default,
    // which drops this input a line below the season and episode fields beside
    // it. The row keeps one input line only while the helper follows the input
    // rather than preceding it.
    const helper = screen.getByText("Use the series IMDb ID.");
    expect(
      imdb.compareDocumentPosition(helper) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it.each(["Search subtitles by IMDb ID", "Search providers by release name"])(
    "opens %s from an empty catalog without showing homepage retrieval controls",
    async (action) => {
      server.use(
        http.get("/api/discover/metadata/search", () =>
          HttpResponse.json({
            data: { ...envelope, status: "unavailable", items: [] },
          }),
        ),
      );
      const { user } = renderDiscover();
      await user.type(screen.getByLabelText("Search"), "missing");
      expect(screen.queryByLabelText("IMDb ID")).toBeNull();
      expect(screen.queryByLabelText("Release name")).toBeNull();
      expect(screen.queryByLabelText("Subtitle language")).toBeNull();
      await user.click(await screen.findByRole("button", { name: action }));
      expect(
        screen.getByLabelText(
          action.includes("IMDb") ? "IMDb ID" : "Release name",
        ),
      ).toBeEnabled();
      expect(
        screen.getByLabelText(
          action.includes("IMDb") ? "IMDb ID" : "Release name",
        ),
      ).toHaveFocus();
      expect(
        screen.getByRole("button", { name: "Find subtitles" }),
      ).toBeDisabled();
      await user.click(
        screen.getByRole("button", { name: "Back to Discover" }),
      );
      expect(screen.getByLabelText("Search")).toHaveValue("missing");
      await user.click(screen.getByLabelText("Search"));
      await waitFor(() =>
        expect(screen.getByLabelText("Search")).toHaveFocus(),
      );
      expect(screen.queryByLabelText("Subtitle language")).toBeNull();
    },
  );

  it("shows only feeds belonging to the selected media scope", async () => {
    const { user } = renderDiscover();
    expect(
      screen.getByRole("heading", { name: "Recent digital releases" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "New episodes" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Movies" }));
    expect(screen.queryByRole("heading", { name: "New episodes" })).toBeNull();
    expect(
      screen.getByRole("heading", { name: "Recent digital releases" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Series" }));
    expect(
      screen.queryByRole("heading", { name: "Recent digital releases" }),
    ).toBeNull();
    expect(
      screen.getByRole("heading", { name: "New episodes" }),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "All media" }));
    expect(
      screen.getByRole("heading", { name: "Recent digital releases" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "New episodes" }),
    ).toBeInTheDocument();
  });

  it.each([
    { preference: "light", theme: "day" },
    { preference: "dark", theme: "night" },
  ])(
    "follows the resolved $preference appearance in automatic mode",
    async ({ preference, theme }) => {
      const original = window.matchMedia;
      window.matchMedia = vi.fn((query) => ({
        ...original(query),
        matches:
          query === "(prefers-color-scheme: dark)" && preference === "dark",
      }));
      try {
        renderDiscover();
        await waitFor(() =>
          expect(
            screen.getByRole("region", { name: "Discover" }),
          ).toHaveAttribute("data-theme", theme),
        );
        expect(document.documentElement).toHaveAttribute(
          "data-mantine-color-scheme",
          preference,
        );
      } finally {
        window.matchMedia = original;
      }
    },
  );

  it("requires explicit language and Find subtitles; appearance and query refetch never search", async () => {
    const requests: unknown[] = [];
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        requests.push(await request.json());
        return HttpResponse.json(snapshot());
      }),
    );
    const { user } = renderDiscover();
    expect(screen.queryByLabelText("IMDb ID")).toBeNull();
    expect(screen.queryByLabelText("Subtitle language")).toBeNull();
    expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull();
    await selectTitle(user);
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    expect(selectInput("Subtitle language")).toHaveValue("");
    await pickOption(user, "Subtitle language", "English");
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    await queryClient.refetchQueries({ type: "active" });
    expect(requests).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    expect(
      await screen.findByText("The.Matrix.1999.1080p"),
    ).toBeInTheDocument();
    expect(requests).toEqual([
      {
        media_type: "movie",
        imdb_id: "tt0133093",
        title: "The Matrix",
        year: 1999,
        language: "eng",
        refresh: false,
      },
    ]);
    await queryClient.refetchQueries({ type: "active" });
    expect(requests).toHaveLength(1);
    await user.click(
      screen.getByLabelText("Match evidence for The.Matrix.1999.1080p"),
    );
    expect(screen.getByText("Hearing-impaired cues")).toBeVisible();
    expect(
      screen
        .getAllByText("Unknown")
        .some(
          (element) =>
            element.previousElementSibling?.textContent ===
            "Hearing-impaired cues",
        ),
    ).toBe(true);
  });

  it("keeps the saved task through settings navigation and appearance changes", async () => {
    server.use(
      http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
    );
    const { user, router } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("The.Matrix.1999.1080p");
    await user.click(screen.getByRole("link", { name: "Subtitle Hub" }));
    await act(async () => {
      await router.navigate(-1);
    });
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  });

  it("retains prior results and checked time after a failed refresh", async () => {
    let attempt = 0;
    server.use(
      http.post("/api/discover/search", () => {
        attempt += 1;
        return attempt === 1
          ? HttpResponse.json(snapshot())
          : HttpResponse.json({ message: "Unavailable" }, { status: 503 });
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("The.Matrix.1999.1080p");
    await user.click(screen.getByRole("button", { name: "Search again" }));
    expect(await screen.findByText(/Refresh failed/)).toBeInTheDocument();
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
    expect(
      screen.getAllByText(readableTime("2026-09-08T10:00:00Z"))[0],
    ).toHaveAttribute("datetime", "2026-09-08T10:00:00Z");
  });

  it("accepts a partial server snapshot without resurrecting an omitted handle", async () => {
    const first = snapshot("Evicted result");
    const retained = {
      ...first.results[0],
      id: "still-valid",
      release: "Usable result",
    };
    first.results.push(retained);
    let attempt = 0;
    server.use(
      http.post("/api/discover/search", () => {
        attempt += 1;
        return HttpResponse.json(
          attempt === 1
            ? first
            : {
                ...first,
                status: "partial",
                cache_status: "stale",
                results: [{ ...retained, stale: true }],
                coverage: {
                  ...first.coverage,
                  complete: false,
                  completed_count: 0,
                  providers: [
                    {
                      ...first.coverage.providers[0],
                      status: "authentication_required",
                      result_count: 0,
                    },
                  ],
                },
              },
        );
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("Evicted result");
    await user.click(screen.getByRole("button", { name: "Search again" }));
    await screen.findByText("Sign in to this provider in provider settings");
    expect(screen.queryByText("Evicted result")).not.toBeInTheDocument();
    expect(screen.getByText("Usable result")).toBeInTheDocument();
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
    expect(
      screen.getAllByText(readableTime(first.checked_at))[0],
    ).toHaveAttribute("datetime", first.checked_at);
  });

  it.each([false, true])(
    "drops expired handles after transport failure, with usable remainder %s",
    async (hasUsable) => {
      const first = snapshot("Expired result");
      const expiry = Date.now() + 60 * 60 * 1000;
      first.results[0].expires_at = new Date(expiry).toISOString();
      if (hasUsable)
        first.results.push({
          ...first.results[0],
          id: "still-valid",
          release: "Usable result",
          expires_at: new Date(expiry + 60 * 60 * 1000).toISOString(),
        });
      let attempt = 0;
      server.use(
        http.post("/api/discover/search", () => {
          attempt += 1;
          return attempt === 1
            ? HttpResponse.json(first)
            : HttpResponse.json({}, { status: 503 });
        }),
      );
      const { user } = renderDiscover();
      await selectTarget(user);
      await user.click(screen.getByRole("button", { name: "Find subtitles" }));
      await screen.findByText("Expired result");
      const now = vi.spyOn(Date, "now").mockReturnValue(expiry + 1);
      try {
        await user.click(screen.getByRole("button", { name: "Search again" }));
        await screen.findByText(hasUsable ? /Refresh failed/ : /Search failed/);
        expect(screen.queryByText("Expired result")).not.toBeInTheDocument();
        expect(
          screen.queryByText(/No subtitles matched this title and language/),
        ).not.toBeInTheDocument();
        if (hasUsable)
          expect(screen.getByText("Usable result")).toBeInTheDocument();
        expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
        expect(
          screen.getAllByText(readableTime(first.checked_at))[0],
        ).toHaveAttribute("datetime", first.checked_at);
        if (!hasUsable) {
          let complete: (() => void) | undefined;
          server.use(
            http.post("/api/discover/search", async () => {
              await new Promise<void>((resolve) => {
                complete = resolve;
              });
              return HttpResponse.json({
                ...first,
                results: [],
                coverage: {
                  ...first.coverage,
                  providers: [
                    {
                      ...first.coverage.providers[0],
                      status: "empty",
                      result_count: 0,
                    },
                  ],
                },
              });
            }),
          );
          await user.click(
            screen.getByRole("button", { name: "Search again" }),
          );
          await waitFor(() => expect(complete).toBeDefined());
          expect(
            screen.getByRole("progressbar", { name: "Providers checked" }),
          ).toBeInTheDocument();
          expect(
            screen.queryByText(/No subtitles matched this title and language/),
          ).not.toBeInTheDocument();
          complete?.();
          expect(
            await screen.findByText(
              /No subtitles matched this title and language/,
            ),
          ).toBeInTheDocument();
        }
      } finally {
        now.mockRestore();
      }
    },
  );

  it("ignores a late response after the language changes, and logout clears the task", async () => {
    let release: (() => void) | undefined;
    server.use(
      http.post("/api/discover/search", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(snapshot());
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(release).toBeDefined());
    await pickOption(user, "Subtitle language", "Hungarian");
    release?.();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Find subtitles" }),
      ).toBeEnabled(),
    );
    expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
    setAuthenticated(false);
    await waitFor(() => expect(screen.queryByLabelText("IMDb ID")).toBeNull());
    expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull();
  });

  it("blocks malformed IMDb identity and incomplete exact episodes", async () => {
    const { user } = renderDiscover();
    expect(screen.queryByLabelText("IMDb ID")).toBeNull();
    expect(screen.queryByLabelText("Subtitle language")).toBeNull();
    expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull();
    await selectTitle(user);
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    await pickOption(user, "Subtitle language", "English");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Back to Discover" }));
    expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull();
    await selectShow(user);
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    await user.click(
      screen.getByRole("button", { name: "Enter episode numbers manually" }),
    );
    // Prefilled from the link, so the numbers are replaced, not appended.
    await user.clear(screen.getByRole("textbox", { name: "Season" }));
    await user.type(screen.getByRole("textbox", { name: "Season" }), "0");
    await user.clear(screen.getByRole("textbox", { name: "Episode" }));
    await user.type(screen.getByRole("textbox", { name: "Episode" }), "3");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    await user.click(
      screen.getByLabelText(
        "I confirm this series IMDb ID and the manual season and episode numbers",
      ),
    );
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeEnabled();
  });
});

it("distinguishes successful empty, partial, failed and provider setup states", async () => {
  const full = snapshot();
  const responses = [
    {
      ...full,
      results: [],
      coverage: {
        ...full.coverage,
        providers: [
          { ...full.coverage.providers[0], status: "empty", result_count: 0 },
        ],
      },
    },
    {
      ...full,
      status: "partial",
      coverage: {
        ...full.coverage,
        complete: false,
        configured_count: 2,
        providers: [
          ...full.coverage.providers,
          {
            provider: "needs-login",
            status: "authentication_required",
            result_count: 0,
            reason: "authentication_required",
            elapsed_ms: 1,
            retry_at: null,
          },
        ],
      },
    },
    {
      ...full,
      status: "failed",
      results: [],
      coverage: {
        complete: false,
        configured_count: 1,
        completed_count: 0,
        providers: [
          { ...full.coverage.providers[0], status: "timeout", result_count: 0 },
        ],
      },
    },
    {
      ...full,
      status: "failed",
      results: [],
      coverage: {
        complete: false,
        configured_count: 0,
        completed_count: 0,
        providers: [],
      },
    },
  ];
  server.use(
    http.post("/api/discover/search", () =>
      HttpResponse.json(responses.shift()),
    ),
  );
  const { user } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  expect(await screen.findByText(/No subtitles matched/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Search again" }));
  expect(
    screen.queryByText(/Some providers could not complete/),
  ).not.toBeInTheDocument();
  await screen.findByText("The.Matrix.1999.1080p");
  await user.click(screen.getByText(/Search details/, { selector: "summary" }));
  expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  expect(
    screen.getByText("Sign in to this provider in provider settings"),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
  await user.click(screen.getByRole("button", { name: "Search again" }));
  expect(await screen.findByText(/No provider completed/)).toBeInTheDocument();
  expect(screen.getByText("Provider search timed out")).toBeInTheDocument();
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Search again" }));
  expect(
    await screen.findByText("Set up a subtitle provider"),
  ).toBeInTheDocument();
});

it("remembers only the explicit language and works when browser storage is blocked", async () => {
  const original = Storage.prototype.setItem;
  const blocked = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(function (this: Storage, key, value) {
      if (key === "bazarr.discover.subtitle-language")
        throw new DOMException("Storage blocked", "SecurityError");
      original.call(this, key, value);
    });
  try {
    const { user, router } = renderDiscover();
    await selectTarget(user);
    expect(screen.getByText(/remembered for this visit/)).toBeInTheDocument();
    expect(selectInput("Subtitle language")).toHaveValue("English");
    await user.click(screen.getByRole("link", { name: "Subtitle Hub" }));
    await act(async () => {
      await router.navigate(-1);
    });
    expect(selectInput("Subtitle language")).toHaveValue("English");
  } finally {
    blocked.mockRestore();
  }
});

// The seeded language is written when the first search uses it, and that write
// is the same evidence about storage as any other. Its answer was being thrown
// away, so a reader whose storage refuses writes was told nothing and believed
// the seeded language would be there next time.
it("says a seeded language is session-only once the search tries to remember it", async () => {
  server.use(
    http.get("/api/system/languages/profiles", () =>
      HttpResponse.json([
        {
          profileId: 1,
          name: "English",
          cutoff: null,
          items: [
            {
              id: 1,
              language: "en",
              audio_exclude: "False",
              hi: "False",
              forced: "False",
            },
          ],
          mustContain: [],
          mustNotContain: [],
          originalFormat: false,
          tag: null,
        },
      ]),
    ),
    http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
  );
  const original = Storage.prototype.setItem;
  const blocked = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(function (this: Storage, key, value) {
      if (key === "bazarr.discover.subtitle-language")
        throw new DOMException("Storage blocked", "SecurityError");
      original.call(this, key, value);
    });
  try {
    const { user } = renderDiscover();
    await selectTitle(user);
    await waitFor(() =>
      expect(selectInput("Subtitle language")).toHaveValue("English"),
    );
    // Seeding alone writes nothing, so there is nothing to report yet.
    expect(screen.queryByText(/remembered for this visit/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    expect(
      await screen.findByText(/remembered for this visit/),
    ).toBeInTheDocument();
  } finally {
    blocked.mockRestore();
  }
});

it("restores an explicit preference without deriving one from enabled library languages", async () => {
  localStorage.setItem("bazarr.discover.subtitle-language", "eng");
  const { user } = renderDiscover();
  expect(screen.queryByLabelText("IMDb ID")).toBeNull();
  expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull();
  await selectTitle(user);
  // The stored code is shown by its name once the language list has loaded.
  await findSelectInput("Subtitle language");
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("English"),
  );
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeEnabled();
});

it("preserves results when the shell replaces its router", async () => {
  server.use(
    http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
  );
  const { user, view } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByText("The.Matrix.1999.1080p");
  const replacement = createMemoryRouter(
    [{ path: "/discover", element: <Discover /> }],
    { initialEntries: ["/discover"] },
  );
  view.rerender(
    <AllProviders>
      <RouterProvider router={replacement} />
    </AllProviders>,
  );
  expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
});

it("does not restore a logged-out task when its last request finishes", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.post("/api/discover/search", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json(snapshot());
    }),
  );
  const { user } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(finish).toBeDefined());
  setAuthenticated(false);
  await waitFor(() => expect(screen.queryByLabelText("IMDb ID")).toBeNull());
  finish?.();
  await waitFor(() =>
    expect(screen.queryByRole("button", { name: "Find subtitles" })).toBeNull(),
  );
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
});

it("keeps identified title retrieval focused on language and search", async () => {
  const { user } = renderDiscover();
  await selectTitle(user);
  expect(screen.getByLabelText("Search")).toBeVisible();
  expect(screen.getByLabelText("IMDb ID")).not.toBeVisible();
  expect(
    screen.queryByText("Match a copy in your library"),
  ).not.toBeInTheDocument();
  expect(screen.queryByText(/Metadata fetched/)).not.toBeInTheDocument();
  expect(
    screen.queryByText(/Select a title and a subtitle language/),
  ).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeVisible();
});

// The boundary this pair pins: the title's own way back edits the URL inside
// the Discover route, so it is history navigation with the page still mounted.
// Leaving the route altogether is the cases below it, where the page is filed
// on the way out.
it("restores completed subtitle results through Back and Forward inside Discover without another search", async () => {
  let searches = 0;
  server.use(
    http.post("/api/discover/search", () => {
      searches++;
      return HttpResponse.json(snapshot());
    }),
  );
  const { user, router } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByText("The.Matrix.1999.1080p");
  const detail = router.state.location.search;
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
  await act(async () => {
    await router.navigate(-1);
  });
  await screen.findByText("The.Matrix.1999.1080p");
  expect(router.state.location.search).toBe(detail);
  expect(selectInput("Subtitle language")).toHaveValue("English");
  expect(searches).toBe(1);
  await act(async () => {
    await router.navigate(1);
  });
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
  await act(async () => {
    await router.navigate(-1);
  });
  await screen.findByText("The.Matrix.1999.1080p");
  expect(searches).toBe(1);
  setAuthenticated(false);
  await waitFor(() =>
    expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument(),
  );
  setAuthenticated(true);
  await act(async () => {
    await router.navigate(1);
  });
  await act(async () => {
    await router.navigate(-1);
  });
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
});

/**
 * A provider answer the case holds open, for the two races where the reader is
 * on another route while the search is out. `arrive` lets the answer land.
 */
function pendingSearch(onRequest: () => void) {
  let arrive: (() => void) | undefined;
  const held = new Promise<void>((resolve) => {
    arrive = resolve;
  });
  return {
    handler: http.post("/api/discover/search", async () => {
      onRequest();
      await held;
      return HttpResponse.json(snapshot());
    }),
    arrive: () => arrive?.(),
  };
}

it("keeps results that arrive while the reader is on another route", async () => {
  let searches = 0;
  const search = pendingSearch(() => {
    searches++;
  });
  server.use(search.handler);
  const { user, router } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  // Leaving the route files the title's page while its search is still out.
  await user.click(screen.getByRole("link", { name: "History" }));
  await screen.findByText("Local history page");
  await act(async () => {
    search.arrive();
  });
  await act(async () => {
    await router.navigate(-1);
  });
  await screen.findByText("The.Matrix.1999.1080p");
  expect(searches).toBe(1);
});

it("keeps a search running while the reader is on another route", async () => {
  let searches = 0;
  const search = pendingSearch(() => {
    searches++;
  });
  server.use(search.handler);
  const { user, router } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await user.click(screen.getByRole("link", { name: "History" }));
  await screen.findByText("Local history page");
  // Coming back before the provider answers must not retire the search.
  await act(async () => {
    await router.navigate(-1);
  });
  await act(async () => {
    search.arrive();
  });
  await screen.findByText("The.Matrix.1999.1080p");
  expect(searches).toBe(1);
});

describe("Discover return from local work", () => {
  const emptyActivity = {
    availability: "available",
    observed_at: "2026-09-01T12:00:00Z",
    complete: true,
    truncated: false,
    unknown_sources: [],
    running_count: 0,
    queued_count: 0,
    scheduled_count: 0,
    running: [],
    queued: [],
    scheduled: [],
  };

  const emptyWanted = {
    availability: "available",
    observed_at: "2026-09-01T12:00:00Z",
    requirements: 0,
    episode_requirements: 0,
    movie_requirements: 0,
    media_count: 0,
    unknown_media_count: 0,
    complete: true,
    qualifications: [],
    by_instance: [],
  };

  function summary(overrides: Record<string, unknown> = {}) {
    return {
      generated_at: "2026-09-01T12:00:00Z",
      state: "quiet",
      activity: emptyActivity,
      wanted: emptyWanted,
      arrivals: [],
      arrivals_status: {
        availability: "available",
        observed_at: "2026-09-01T12:00:00Z",
        complete: true,
        truncated: false,
        candidate_limit: 25,
        display_limit: 4,
        qualifications: [],
      },
      attention: {
        availability: "available",
        observed_at: "2026-09-01T12:00:00Z",
        complete: true,
        unknown_sources: [],
        items: [],
      },
      onboarding: {
        availability: "available",
        observed_at: "2026-09-01T12:00:00Z",
        complete: true,
        items: [],
      },
      ...overrides,
    };
  }

  let served = summary();

  beforeEach(() => {
    served = summary();
    server.use(
      http.get("/api/discover/summary", () => HttpResponse.json(served)),
    );
  });

  function browseDiscover() {
    const router = createMemoryRouter(
      [
        {
          path: "/discover",
          element: (
            <>
              <DetailEntry />
              <Discover />
            </>
          ),
        },
        { path: "/system/tasks", element: <div>Local activity page</div> },
        { path: "/history/series", element: <div>Local history page</div> },
      ],
      { initialEntries: ["/discover"] },
    );
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    return router;
  }

  /** Enters the title detail on demand. The homepage carries no retrieval
   * controls, so focus and scroll restoration tests reach the detail this way
   * instead of by typing into a homepage form. Setting both sides together
   * keeps the detail on its retrieval page: typing an IMDb identifier into the
   * detail form would clear the selection and return to the homepage. */
  function DetailEntry() {
    const navigate = useNavigate();
    const { updateBrowsing, updateDraft } = useDiscover();
    return (
      <button
        type="button"
        onClick={() => {
          void navigate("/discover?movie=900");
          updateBrowsing({
            selectedId: 900,
            selectedSource: "tmdb",
            selectedType: "movie",
            selectedSeason: null,
            selectedEpisode: null,
          });
          updateDraft({
            mediaType: "movie",
            imdbId: "tt1234567",
            language: "eng",
            season: "",
            episode: "",
          });
        }}
      >
        Enter detail
      </button>
    );
  }

  function discoverFixtures() {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", use_sonarr: false, use_radarr: false },
          discover: {
            tmdb_configured: false,
            metadata_revision: "summary-one",
            locale: "en-US",
          },
        }),
      ),
      http.get("/api/system/languages", () =>
        HttpResponse.json([
          { name: "English", code3: "eng", code2: "en", enabled: false },
        ]),
      ),
    );
  }

  /**
   * Drives window scroll, paint frames and element geometry the way the page
   * owner sees them. jsdom reports a zero scroll offset and never paints, so a
   * restoration that runs in a requestAnimationFrame is invisible without this.
   */
  function scrollHarness(controlTop: number, headerHeight = 112) {
    const scrollDescriptor = Object.getOwnPropertyDescriptor(window, "scrollY");
    const heightDescriptor = Object.getOwnPropertyDescriptor(
      window,
      "innerHeight",
    );
    // The page reconciles against the real app shell header, which these focused
    // renders do not mount. Give it one so the reveal path is actually exercised.
    const shell = document.createElement("header");
    shell.className = "mantine-AppShell-header";
    document.body.append(shell);
    const nativeRect = HTMLElement.prototype.getBoundingClientRect;
    const setScroll = (top: number) =>
      Object.defineProperty(window, "scrollY", {
        configurable: true,
        value: top,
      });
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (["SELECT", "INPUT", "BUTTON"].includes(this.tagName))
          return new DOMRect(0, controlTop - window.scrollY, 200, 44);
        if (this.tagName === "HEADER")
          return new DOMRect(0, 0, 320, headerHeight);
        return nativeRect.call(this);
      });
    const scrollTo = vi
      .spyOn(window, "scrollTo")
      .mockImplementation((options: number | ScrollToOptions) => {
        if (typeof options === "object") setScroll(options.top ?? 0);
      });
    const frames = new Map<number, FrameRequestCallback>();
    let frameId = 0;
    const requestFrame = vi
      .spyOn(window, "requestAnimationFrame")
      .mockImplementation((callback) => {
        frames.set(++frameId, callback);
        return frameId;
      });
    const cancelFrame = vi
      .spyOn(window, "cancelAnimationFrame")
      .mockImplementation((id) => {
        frames.delete(id);
      });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: 900,
    });
    setScroll(0);
    return {
      setScroll,
      paint: () =>
        act(() => {
          const pending = [...frames.values()];
          frames.clear();
          pending.forEach((callback) => callback(performance.now()));
        }),
      pending: () => frames.size,
      restore: () => {
        shell.remove();
        rect.mockRestore();
        scrollTo.mockRestore();
        requestFrame.mockRestore();
        cancelFrame.mockRestore();
        if (scrollDescriptor)
          Object.defineProperty(window, "scrollY", scrollDescriptor);
        if (heightDescriptor)
          Object.defineProperty(window, "innerHeight", heightDescriptor);
      },
    };
  }

  it("shows search, the start of discovery and the summary together", async () => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          // A connected library, because the library half of the page is only
          // shown to an install that has one.
          general: { theme: "auto", use_sonarr: true, use_radarr: false },
          discover: {
            tmdb_configured: false,
            metadata_revision: "summary-one",
            locale: "en-US",
          },
        }),
      ),
      http.get("/api/system/languages", () =>
        HttpResponse.json([
          { name: "English", code3: "eng", code2: "en", enabled: false },
        ]),
      ),
    );
    served = summary({
      wanted: { ...emptyWanted, requirements: 3, media_count: 2 },
    });
    browseDiscover();

    expect(await screen.findByText("Your library")).toBeVisible();
    expect(screen.getByRole("heading", { name: "Discover" })).toBeVisible();
    // The homepage carries no retrieval controls. Search and the summary sit
    // together there; the retrieval controls live on the detail.
    expect(screen.queryByLabelText("Subtitle language")).toBeNull();
    expect(
      screen.queryByRole("button", { name: /Find subtitles/ }),
    ).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Enter detail" }));
    expect(await findSelectInput("Subtitle language")).toBeVisible();
    expect(
      screen.getByRole("button", { name: /Find subtitles/ }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Your library")).not.toBeInTheDocument();
  });

  it("records a return target so leaving for local work restores Discover", async () => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", use_sonarr: false, use_radarr: false },
          discover: {
            tmdb_configured: false,
            metadata_revision: "summary-one",
            locale: "en-US",
          },
        }),
      ),
      http.get("/api/system/languages", () =>
        HttpResponse.json([
          { name: "English", code3: "eng", code2: "en", enabled: false },
        ]),
      ),
    );
    const router = browseDiscover();
    await userEvent.click(screen.getByRole("button", { name: "Enter detail" }));
    const imdb = await screen.findByLabelText("IMDb ID");
    expect(imdb).toHaveValue("tt1234567");

    await userEvent.click(screen.getByRole("link", { name: "History" }));
    await waitFor(() =>
      expect(screen.getByText("Local history page")).toBeVisible(),
    );

    await waitFor(() => router.navigate(-1));
    await waitFor(() =>
      expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt1234567"),
    );
    expect(screen.queryByText("Your Bazarr+")).not.toBeInTheDocument();
  });

  it("restores the retrieval control and the scroll offset after returning from local work", async () => {
    discoverFixtures();
    // The control sits comfortably in view at the saved offset, so the exact
    // offset survives and no reveal adjustment is needed.
    const harness = scrollHarness(3338);
    try {
      const router = browseDiscover();
      await userEvent.click(
        screen.getByRole("button", { name: "Enter detail" }),
      );
      const language = await findSelectInput("Subtitle language");
      await act(async () => {
        language.focus();
        language.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      harness.setScroll(3038);
      await act(async () => {
        window.dispatchEvent(new Event("scroll"));
      });

      await userEvent.click(screen.getByRole("link", { name: "History" }));
      await waitFor(() =>
        expect(screen.getByText("Local history page")).toBeVisible(),
      );
      // The browser applies its own offset on a history return.
      harness.setScroll(742);

      await waitFor(() => router.navigate(-1));
      await findSelectInput("Subtitle language");
      harness.paint();

      const restored = selectInput("Subtitle language");
      expect(restored).toHaveFocus();
      expect(window.scrollY).toBe(3038);
      const bounds = restored.getBoundingClientRect();
      expect(bounds.top).toBeGreaterThanOrEqual(112);
      expect(bounds.bottom).toBeLessThanOrEqual(900);
    } finally {
      harness.restore();
    }
  });

  it("reveals the restored control when the saved offset would hide it behind the shell", async () => {
    discoverFixtures();
    // The control sits under the app shell header at the saved offset.
    const harness = scrollHarness(3000);
    try {
      const router = browseDiscover();
      await userEvent.click(
        screen.getByRole("button", { name: "Enter detail" }),
      );
      const imdb = await screen.findByLabelText("IMDb ID");
      await act(async () => {
        imdb.focus();
        imdb.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      harness.setScroll(2980);
      await act(async () => {
        window.dispatchEvent(new Event("scroll"));
      });
      await userEvent.click(screen.getByRole("link", { name: "History" }));
      await waitFor(() =>
        expect(screen.getByText("Local history page")).toBeVisible(),
      );
      harness.setScroll(0);
      await waitFor(() => router.navigate(-1));
      await screen.findByLabelText("IMDb ID");
      harness.paint();

      const restored = screen.getByLabelText("IMDb ID");
      expect(restored).toHaveFocus();
      // Reconciled up from the saved 2980 so the control clears the header.
      expect(window.scrollY).toBeLessThan(2980);
      expect(restored.getBoundingClientRect().top).toBeGreaterThanOrEqual(112);
    } finally {
      harness.restore();
    }
  });

  it("never steals focus or scroll on a first visit", async () => {
    discoverFixtures();
    const harness = scrollHarness(3040);
    try {
      browseDiscover();
      await userEvent.click(
        screen.getByRole("button", { name: "Enter detail" }),
      );
      await findSelectInput("Subtitle language");
      // Opening a title schedules its requested scroll to the top.
      harness.paint();
      expect(selectInput("Subtitle language")).not.toHaveFocus();
      expect(screen.getByLabelText("IMDb ID")).not.toHaveFocus();
      expect(window.scrollY).toBe(0);
    } finally {
      harness.restore();
    }
  });

  it("cancels the restoration when the page leaves again before paint", async () => {
    discoverFixtures();
    const harness = scrollHarness(3040);
    try {
      const router = browseDiscover();
      await userEvent.click(
        screen.getByRole("button", { name: "Enter detail" }),
      );
      const language = await findSelectInput("Subtitle language");
      await act(async () => {
        language.focus();
        language.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      harness.setScroll(3038);
      await act(async () => {
        window.dispatchEvent(new Event("scroll"));
      });
      await userEvent.click(screen.getByRole("link", { name: "History" }));
      await waitFor(() =>
        expect(screen.getByText("Local history page")).toBeVisible(),
      );
      harness.setScroll(0);
      await waitFor(() => router.navigate(-1));
      await findSelectInput("Subtitle language");
      await waitFor(() => router.navigate("/system/tasks"));
      await waitFor(() =>
        expect(screen.getByText("Local activity page")).toBeVisible(),
      );
      harness.paint();
      expect(window.scrollY).toBe(0);
    } finally {
      harness.restore();
    }
  });

  it("restores a control in the results region, which no other owner claims", async () => {
    discoverFixtures();
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        const context = (await request.json()) as Record<string, unknown>;
        return HttpResponse.json({
          search_id: "return-search",
          context: { ...context, matching_mode: "title" },
          status: "complete",
          checked_at: "2026-09-08T10:00:00Z",
          attempted_at: "2026-09-08T10:00:00Z",
          cache_status: "fresh",
          coverage: {
            complete: true,
            configured_count: 1,
            completed_count: 1,
            providers: [
              {
                provider: "catalog-fixture",
                status: "success",
                result_count: 1,
                elapsed_ms: 5,
                reason: null,
                retry_at: null,
              },
            ],
          },
          results: [
            {
              id: "return-result",
              search_id: "return-search",
              provider: "catalog-fixture",
              language: "eng",
              language_variant: null,
              release: "Northern.Light.S02E05.1080p.WEB-DL",
              uploader: null,
              scope: "unknown",
              hearing_impaired: null,
              matches: ["imdb_id"],
              compatibility_score: 10,
              compatibility_score_max: 119,
              rating: null,
              checked_at: "2026-09-08T10:00:00Z",
              expires_at: "2099-01-01T00:00:00Z",
              stale: false,
            },
          ],
        });
      }),
    );
    const harness = scrollHarness(3338);
    try {
      const router = browseDiscover();
      await userEvent.click(
        screen.getByRole("button", { name: "Enter detail" }),
      );
      const imdb = await screen.findByLabelText("IMDb ID");
      expect(imdb).toHaveValue("tt1234567");
      await pickOption(userEvent, "Subtitle language", "English");
      await userEvent.click(
        screen.getByRole("button", { name: /Find subtitles/ }),
      );
      const preview = await screen.findByRole("button", { name: "Preview" });
      await act(async () => {
        preview.focus();
        preview.dispatchEvent(new FocusEvent("focusin", { bubbles: true }));
      });
      harness.setScroll(3038);
      await act(async () => {
        window.dispatchEvent(new Event("scroll"));
      });

      await userEvent.click(screen.getByRole("link", { name: "History" }));
      await waitFor(() =>
        expect(screen.getByText("Local history page")).toBeVisible(),
      );
      harness.setScroll(759);
      await waitFor(() => router.navigate(-1));
      await screen.findByRole("button", { name: "Preview" });
      harness.paint();

      expect(screen.getByRole("button", { name: "Preview" })).toHaveFocus();
      expect(window.scrollY).toBe(3038);
    } finally {
      harness.restore();
    }
  });
});

describe("Discover touch targets on a coarse pointer", () => {
  it("floors every control that measured under 44px, inside a coarse-pointer block", () => {
    const block = coarseTouchBlock(discoverStyles);
    expect(block).not.toBe("");
    // Not a global size increase: the ticket that owns this page rejected
    // raising these on a fine pointer, so the floor is scoped to the finger.
    expect(block).toMatch(/@media \(pointer: coarse\)/);
    // Retaken at 320px with a coarse pointer: the queue's search pills came to
    // 31.4px, a wanted row's title link to 37px, the summary buttons to 40px
    // and the heading links to 18.6 or 20.1px.
    for (const selector of [
      ".discover .wantedLanguages button",
      ".discover .wantedTitle",
      ".discover .heroCta",
      ".discover .heroActions a",
      ".discover .wantedGroupHead a",
      ".discover .libraryHistory a",
      ".discover .attention li > a",
    ]) {
      const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
      expect(block).toMatch(new RegExp(`${escaped}[^}]*min-height: 44px`));
    }
  });

  it("leaves the deliberate fine-pointer sizes where they were set", () => {
    // The pills are sized to WCAG 2.2's 24px minimum on purpose, not to this
    // page's 44px floor, and the floor must not reach past the media query.
    const outside = discoverStyles.replace(
      coarseTouchBlock(discoverStyles),
      "",
    );
    expect(outside).toMatch(/\.wantedLanguages \{[\s\S]*?min-height: 28px/);
    expect(outside).toMatch(/\.heroCta \{[\s\S]*?min-height: 40px/);
  });
});
