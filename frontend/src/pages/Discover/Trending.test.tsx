/* eslint-disable camelcase -- API fixture fields. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import { AllProviders } from "@/providers";
import { act, fireEvent, rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import type { TrendingFeed, TrendingTitle } from "@/types/discover";
import Discover from ".";

const movie: TrendingTitle = {
  source_id: "tmdb:movie:42",
  id: 42,
  media_type: "movie",
  rank: 1,
  title: "Northern Light",
  year: 2008,
  overview: "A journey north.",
  poster_url: null,
  backdrop_url: null,
};
const show: TrendingTitle = {
  ...movie,
  source_id: "tmdb:show:42",
  media_type: "series",
  rank: 3,
  title: "The Long Winter",
  year: 2018,
};
let revision = "feed-one";
let configured = true;
let status: TrendingFeed["status"] = "live";
let requests: string[] = [];
let searches: unknown[] = [];
const stamp = () => new Date().toISOString();
function envelope(kind: string): TrendingFeed {
  return {
    source: "tmdb",
    status,
    configured,
    revision,
    locale: "en-US",
    period: "week",
    scope: "global",
    media_type: kind as TrendingFeed["media_type"],
    service_status: status === "cached" ? "unavailable" : null,
    fetched_at: stamp(),
    last_success: stamp(),
    attempted_at: stamp(),
    expires_at: new Date(Date.now() + 300000).toISOString(),
    stale_until: new Date(Date.now() + 3600000).toISOString(),
    items: ["live", "cached"].includes(status)
      ? [movie, show].filter(
          (item) => kind === "all" || item.media_type === kind,
        )
      : [],
  };
}
function browse(initial = "/discover", fixedShell = false) {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/system/tasks", element: <div>Local activity</div> },
      { path: "/settings/discover", element: <div>Metadata setup</div> },
    ],
    { initialEntries: [initial] },
  );
  rawRender(
    <AllProviders>
      {fixedShell && <header className="mantine-AppShell-header" />}
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}
beforeEach(() => {
  localStorage.clear();
  configured = true;
  revision = "feed-one";
  status = "live";
  requests = [];
  searches = [];
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "auto",
          use_sonarr: false,
          use_radarr: false,
          setup_complete: false,
        },
        discover: {
          tmdb_configured: configured,
          metadata_revision: revision,
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
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "available",
          configured,
          revision,
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/discover/feeds/trending", ({ request }) => {
      const kind = new URL(request.url).searchParams.get("media_type") ?? "all";
      requests.push(kind);
      return HttpResponse.json(envelope(kind));
    }),
    http.get("/api/discover/metadata/movies/42", () =>
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "available",
          configured,
          revision,
          item: {
            ...movie,
            source: "tmdb",
            imdb_id: "tt0080274",
            mapping_status: "resolved",
          },
        },
      }),
    ),
    http.get("/api/discover/metadata/shows/42", () =>
      HttpResponse.json({
        data: {
          source: "tmdb",
          status: "available",
          configured,
          revision,
          item: {
            ...show,
            media_type: "show",
            source: "tmdb",
            imdb_id: "tt0903747",
            tvdb_id: 123,
            seasons: [{ season: 1, title: "Season 1", episode_count: 2 }],
            mapping_status: "resolved",
          },
        },
      }),
    ),
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
});

it("shows sourced weekly global titles without local media or subtitle language", async () => {
  browse();
  expect(
    await screen.findByRole("heading", { name: "Trending this week" }),
  ).toBeInTheDocument();
  expect(
    await screen.findByRole("button", { name: "Explore Northern Light" }),
  ).toBeEnabled();
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  expect(screen.getByLabelText("Subtitle language")).toHaveValue("");
  expect(screen.getByRole("img", { name: "TMDB" })).toBeInTheDocument();
  expect(searches).toEqual([]);
});

it("keeps filtering, metadata refresh and movie selection separate from explicit provider submission", async () => {
  const { user } = browse();
  await screen.findByRole("button", { name: "Explore Northern Light" });
  await user.click(screen.getByRole("button", { name: "Movies" }));
  await waitFor(() => expect(requests).toContain("movie"));
  expect(screen.getByRole("button", { name: "Movies" })).toHaveAttribute(
    "aria-pressed",
    "true",
  );
  expect(
    screen.queryByRole("button", { name: /The Long Winter 2018/ }),
  ).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh trending" }));
  await waitFor(() => expect(requests).toHaveLength(3));
  await user.click(
    screen.getByRole("button", { name: "Explore Northern Light" }),
  );
  expect(
    await screen.findByRole("heading", { name: "Northern Light" }),
  ).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  expect(searches).toEqual([]);
  await user.selectOptions(screen.getByLabelText("Subtitle language"), "eng");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({
    media_type: "movie",
    imdb_id: "tt0080274",
    language: "eng",
  });
});

it("selects TMDB series identity and requires explicit episode selection", async () => {
  const { user } = browse();
  await screen.findByRole("button", { name: "Explore Northern Light" });
  await user.click(screen.getByRole("button", { name: "Series" }));
  await user.click(
    await screen.findByRole("button", { name: "Explore The Long Winter" }),
  );
  expect(
    await screen.findByRole("heading", { name: "The Long Winter" }),
  ).toBeInTheDocument();
  expect(await screen.findByLabelText("Choose season")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
  expect(searches).toEqual([]);
});

it("restores the global filter and card focus after details and normal navigation", async () => {
  const { router, user } = browse();
  await screen.findByRole("button", { name: "Explore Northern Light" });
  await user.click(screen.getByRole("button", { name: "Movies" }));
  const opener = await screen.findByRole("button", {
    name: "Explore Northern Light",
  });
  await user.click(opener);
  await screen.findByRole("heading", { name: "Northern Light" });
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Explore Northern Light" }),
    ).toHaveFocus(),
  );
  await act(async () => {
    await router.navigate("/system/tasks");
  });
  await act(async () => {
    await router.navigate("/discover");
  });
  await waitFor(() =>
    expect(screen.getByRole("button", { name: "Movies" })).toHaveAttribute(
      "aria-pressed",
      "true",
    ),
  );
  expect(searches).toEqual([]);
});

it.each(["empty", "unavailable", "authentication_failed", "cached"] as const)(
  "shows the truthful %s feed state and metadata recovery",
  async (value) => {
    status = value;
    const { user, router } = browse();
    if (value === "empty")
      expect(
        await screen.findByText(/No titles in this weekly TMDB feed/),
      ).toBeInTheDocument();
    if (value === "unavailable") {
      expect(
        await screen.findByText(/Weekly trending is temporarily unavailable/),
      ).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", { name: "Refresh trending" }),
      );
    }
    if (value === "authentication_failed") {
      await user.click(
        await screen.findByRole("link", { name: "Set up Discover" }),
      );
      expect(router.state.location.pathname).toBe("/settings/discover");
    }
    if (value === "cached")
      expect(
        await screen.findByText(/dated cached feed is retained/),
      ).toBeInTheDocument();
    expect(searches).toEqual([]);
  },
);

it("does not request trending when metadata is unconfigured", async () => {
  configured = false;
  browse();
  expect(
    await screen.findByRole("link", { name: "Set up Discover" }),
  ).toHaveAttribute("href", "/settings/discover");
  expect(requests).toEqual([]);
  expect(searches).toEqual([]);
});

it("rejects a response for obsolete configuration or another filter", async () => {
  server.use(
    http.get("/api/discover/feeds/trending", () =>
      HttpResponse.json({ ...envelope("series"), revision: "retired" }),
    ),
  );
  browse();
  await waitFor(() =>
    expect(
      screen.queryByText("Loading weekly trending titles."),
    ).not.toBeInTheDocument(),
  );
  expect(
    screen.queryByRole("button", { name: "Explore The Long Winter" }),
  ).not.toBeInTheDocument();
  await act(async () => {
    await queryClient.invalidateQueries({
      queryKey: [QueryKeys.Discover, "metadata"],
    });
  });
  expect(searches).toEqual([]);
});

it("never renders expired fallback items as current trending", async () => {
  server.use(
    http.get("/api/discover/feeds/trending", () =>
      HttpResponse.json({
        ...envelope("all"),
        stale_until: "2000-01-01T00:00:00Z",
      }),
    ),
  );
  browse();
  expect(
    await screen.findByText(/Weekly trending is temporarily unavailable/),
  ).toBeInTheDocument();
  expect(
    screen.queryByRole("button", { name: "Explore Northern Light" }),
  ).not.toBeInTheDocument();
});

it("preserves retrieval-route focus separately from the original trending-card return", async () => {
  const { router, user } = browse();
  await user.click(
    await screen.findByRole("button", { name: "Explore Northern Light" }),
  );
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
  await user.click(screen.getByLabelText("IMDb ID"));
  await act(async () => {
    await router.navigate("/system/tasks");
  });
  await act(async () => {
    await router.navigate("/discover");
  });
  await waitFor(() => expect(screen.getByLabelText("IMDb ID")).toHaveFocus());
  await user.click(screen.getByRole("button", { name: "Back to Discover" }));
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Explore Northern Light" }),
    ).toHaveFocus(),
  );
  expect(searches).toEqual([]);
});

it("restores release retrieval focus after the destination has mounted", async () => {
  const { user, router } = browse();
  await user.click(
    await screen.findByRole("button", { name: "Explore Northern Light" }),
  );
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  const input = screen.getByLabelText("Release name");
  await user.type(input, "Northern.Light.2008.WEB-DL");
  await user.click(screen.getByRole("link", { name: "Activity" }));
  await screen.findByText("Local activity");
  expect(screen.queryByLabelText("Release name")).not.toBeInTheDocument();
  await act(async () => {
    await router.navigate(-1);
  });
  await waitFor(() =>
    expect(screen.getByLabelText("Release name")).toHaveFocus(),
  );
  expect(screen.getByLabelText("Release name")).toHaveValue(
    "Northern.Light.2008.WEB-DL",
  );
  expect(searches).toEqual([]);
});

it("keeps the current card position when its scroll event is still pending and restores only once", async () => {
  const { user } = browse();
  await screen.findByRole("button", { name: "Explore Northern Light" });
  const oldScroll = Object.getOwnPropertyDescriptor(window, "scrollY");
  const scrollTo = vi.spyOn(window, "scrollTo");
  try {
    const card = screen.getByRole("button", {
      name: /Artwork unavailable.*Northern Light/,
    });
    Object.defineProperty(window, "scrollY", {
      configurable: true,
      value: 1162,
    });
    act(() => card.focus());
    fireEvent.scroll(window);
    Object.defineProperty(window, "scrollY", {
      configurable: true,
      value: 1083,
    });
    fireEvent.click(card);
    await screen.findByRole("heading", { name: "Northern Light" });
    scrollTo.mockClear();
    await user.click(screen.getByRole("button", { name: "Back to Discover" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", {
          name: /Artwork unavailable.*Northern Light/,
        }),
      ).toHaveFocus(),
    );
    expect(scrollTo).toHaveBeenLastCalledWith({
      top: 1083,
      behavior: "instant",
    });
    expect(scrollTo).toHaveBeenCalledTimes(1);
  } finally {
    if (oldScroll) Object.defineProperty(window, "scrollY", oldScroll);
    scrollTo.mockRestore();
  }
});

it.each([
  {
    mode: "title",
    documentTop: 1201,
    savedScroll: 0,
    viewport: 900,
    shell: 91,
  },
  {
    mode: "release",
    documentTop: 730,
    savedScroll: 0,
    viewport: 568,
    shell: 112,
  },
  {
    mode: "release",
    documentTop: 730,
    savedScroll: 700,
    viewport: 568,
    shell: 112,
  },
  {
    mode: "title",
    documentTop: 1000,
    savedScroll: 720,
    viewport: 900,
    shell: 91,
  },
])(
  "keeps a restored retrieval control visible: $mode at $savedScroll in $viewport",
  async ({ mode, documentTop, savedScroll, viewport, shell }) => {
    const scrollDescriptor = Object.getOwnPropertyDescriptor(window, "scrollY");
    const heightDescriptor = Object.getOwnPropertyDescriptor(
      window,
      "innerHeight",
    );
    const nativeRect = HTMLElement.prototype.getBoundingClientRect;
    const targetId =
      mode === "release" ? "discover-release-query" : "discover-imdb-id";
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.id === targetId)
          return new DOMRect(0, documentTop - window.scrollY, 200, 44);
        if (this.tagName === "HEADER") return new DOMRect(0, 0, 320, shell);
        return nativeRect.call(this);
      });
    const scrollTo = vi
      .spyOn(window, "scrollTo")
      .mockImplementation((options: number | ScrollToOptions) => {
        if (typeof options === "object")
          Object.defineProperty(window, "scrollY", {
            configurable: true,
            value: options.top ?? 0,
          });
      });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: viewport,
    });
    try {
      const { user, router } = browse("/discover", true);
      await user.click(
        await screen.findByRole("button", { name: "Explore Northern Light" }),
      );
      if (mode === "release") {
        await user.click(
          screen.getByRole("button", {
            name: "Search providers by release name",
          }),
        );
        await user.type(
          screen.getByLabelText("Release name"),
          "Northern.Light.2008.WEB-DL",
        );
      } else
        await waitFor(() =>
          expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
        );
      Object.defineProperty(window, "scrollY", {
        configurable: true,
        value: savedScroll,
      });
      await user.click(
        screen.getByLabelText(mode === "release" ? "Release name" : "IMDb ID"),
      );
      await user.click(screen.getByRole("link", { name: "Activity" }));
      await screen.findByText("Local activity");
      scrollTo.mockClear();
      await act(async () => {
        await router.navigate(-1);
      });
      const input = await screen.findByLabelText(
        mode === "release" ? "Release name" : "IMDb ID",
      );
      await waitFor(() => expect(input).toHaveFocus());
      const bounds = input.getBoundingClientRect();
      expect(bounds.top).toBeGreaterThanOrEqual(shell);
      expect(bounds.bottom).toBeLessThanOrEqual(viewport);
      if (
        documentTop - savedScroll >= shell &&
        documentTop - savedScroll + 44 <= viewport
      ) {
        expect(window.scrollY).toBe(savedScroll);
        expect(scrollTo).toHaveBeenCalledTimes(1);
      }
      expect(input).toHaveValue(
        mode === "release" ? "Northern.Light.2008.WEB-DL" : "tt0080274",
      );
      expect(searches).toEqual([]);
    } finally {
      rect.mockRestore();
      scrollTo.mockRestore();
      if (scrollDescriptor)
        Object.defineProperty(window, "scrollY", scrollDescriptor);
      if (heightDescriptor)
        Object.defineProperty(window, "innerHeight", heightDescriptor);
    }
  },
);

it.each(["title", "release"])(
  "restores %s after native history scrolling and cancels when the route leaves before paint",
  async (mode) => {
    const { user, router } = browse("/discover", true);
    await user.click(
      await screen.findByRole("button", { name: "Explore Northern Light" }),
    );
    if (mode === "release") {
      await user.click(
        screen.getByRole("button", {
          name: "Search providers by release name",
        }),
      );
      await user.type(screen.getByLabelText("Release name"), "Northern.WEB-DL");
    } else
      await waitFor(() =>
        expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
      );
    const label = mode === "release" ? "Release name" : "IMDb ID";
    await user.click(screen.getByLabelText(label));
    await user.click(screen.getByRole("link", { name: "Activity" }));
    await screen.findByText("Local activity");
    const scrollDescriptor = Object.getOwnPropertyDescriptor(window, "scrollY");
    const heightDescriptor = Object.getOwnPropertyDescriptor(
      window,
      "innerHeight",
    );
    const nativeRect = HTMLElement.prototype.getBoundingClientRect;
    const rect = vi
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockImplementation(function (this: HTMLElement) {
        if (this.tagName === "INPUT")
          return new DOMRect(0, 730 - window.scrollY, 200, 44);
        if (this.tagName === "HEADER") return new DOMRect(0, 0, 320, 112);
        return nativeRect.call(this);
      });
    const setScroll = (top: number) =>
      Object.defineProperty(window, "scrollY", {
        configurable: true,
        value: top,
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
    const paint = () =>
      act(() => {
        const pending = [...frames.values()];
        frames.clear();
        pending.forEach((callback) => callback(performance.now()));
      });
    Object.defineProperty(window, "innerHeight", {
      configurable: true,
      value: 568,
    });
    try {
      await act(async () => {
        await router.navigate(-1);
      });
      const input = screen.getByLabelText(label);
      // Native history restores its old offset after React commits the route.
      setScroll(0);
      paint();
      expect(input).toHaveFocus();
      expect(input.getBoundingClientRect().top).toBeGreaterThanOrEqual(112);
      expect(input.getBoundingClientRect().bottom).toBeLessThanOrEqual(568);
      expect(input).toHaveValue(
        mode === "release" ? "Northern.WEB-DL" : "tt0080274",
      );
      await act(async () => {
        await router.navigate("/system/tasks");
      });
      await act(async () => {
        await router.navigate(-1);
      });
      await act(async () => {
        await router.navigate("/system/tasks");
      });
      scrollTo.mockClear();
      paint();
      expect(screen.queryByLabelText(label)).not.toBeInTheDocument();
      expect(scrollTo).not.toHaveBeenCalled();
      expect(searches).toEqual([]);
    } finally {
      requestFrame.mockRestore();
      cancelFrame.mockRestore();
      rect.mockRestore();
      scrollTo.mockRestore();
      if (scrollDescriptor)
        Object.defineProperty(window, "scrollY", scrollDescriptor);
      if (heightDescriptor)
        Object.defineProperty(window, "innerHeight", heightDescriptor);
    }
  },
);
