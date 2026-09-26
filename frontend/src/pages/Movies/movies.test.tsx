import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import MovieView from ".";

describe("Movies page", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/movies", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );
  });

  it("should render", async () => {
    customRender(<MovieView />);

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Search by title..."),
      ).toBeInTheDocument();
    });
  });

  it("counts one movie as a movie in the toolbar band", async () => {
    const movie: Item.Movie = {
      id: 7,
      radarrId: 7,
      arr_instance_id: 2,
      title: "Glass Harbour",
      path: "/movies/Glass Harbour",
      tags: [],
      monitored: true,
      audio_language: [{ code2: "en", name: "English" }],
      profileId: null,
      fanart: "",
      overview: "",
      imdbId: "",
      alternativeTitles: [],
      poster: "",
      year: "2023",
      subtitles: [],
      missing_subtitles: [],
    };
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({ data: [movie], total: 1 }),
      ),
    );
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    const search = screen.getByPlaceholderText("Search by title...");
    // eslint-disable-next-line testing-library/no-node-access
    const band = search.closest("[data-holds]");
    if (!(band instanceof HTMLElement)) throw new Error("No toolbar band");
    await waitFor(() =>
      expect(within(band).getByRole("status")).toHaveTextContent(/^1 movie$/),
    );
  });
});

/* eslint-disable camelcase -- API fixture fields keep their transport names. */
function movie(
  id: number,
  title: string,
  missing: Subtitle[],
  profileId: number | null = 1,
): Item.Movie {
  return {
    id,
    radarrId: id,
    arr_instance_id: 1,
    title,
    path: `/movies/${title}`,
    tags: [],
    monitored: true,
    audio_language: [{ code2: "en", name: "English" }],
    profileId,
    fanart: "",
    overview: "",
    imdbId: "",
    alternativeTitles: [],
    poster: "",
    year: "2023",
    subtitles: [],
    missing_subtitles: missing,
  };
}

const frenchMissing: Subtitle = {
  code2: "fr",
  name: "French",
  forced: false,
  hi: false,
  path: null,
};

// The Subtitles filter keeps rows whose completeness matches the choice. A
// profiled movie is complete when its missing_subtitles list is empty and
// missing otherwise; a movie with no language profile has nothing to complete,
// so it is untracked and belongs to neither group.
describe("Movies subtitles filter", () => {
  const complete = movie(1, "Glass Harbour", []);
  const missing = movie(2, "Northern Light", [frenchMissing]);

  beforeEach(() => {
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({ data: [complete, missing], total: 2 }),
      ),
    );
  });

  // The band has no role of its own, so it is found as the search field's
  // container, and its shown count is the aria-hidden copy a sighted reader
  // sees (the status region says the same words to assistive technology).
  function band() {
    const search = screen.getByPlaceholderText("Search by title...");
    // eslint-disable-next-line testing-library/no-node-access
    const found = search.closest("[data-holds]");
    if (!(found instanceof HTMLElement)) throw new Error("No toolbar band");
    return found;
  }

  function shownCount() {
    return (
      within(band())
        .queryAllByText(/movies?$/)
        .find((element) => element.getAttribute("aria-hidden") === "true") ??
      null
    );
  }

  async function openFilters(user: ReturnType<typeof userEvent.setup>) {
    await user.click(
      await screen.findByRole("button", { name: "Toggle filters" }),
    );
  }

  // The dropdown never settles in jsdom, so a click focuses without opening and
  // the list stays out of the accessibility tree: open with ArrowDown and query
  // the options hidden, the way the other select-driven page tests do.
  async function pickSubtitles(
    user: ReturnType<typeof userEvent.setup>,
    option: string,
  ) {
    const input = screen.getByRole("combobox", { name: "Subtitles" });
    await user.click(input);
    if (input.getAttribute("aria-expanded") !== "true")
      await user.keyboard("{ArrowDown}");
    const controlled = input.getAttribute("aria-controls");
    const listboxes = await screen.findAllByRole("listbox", { hidden: true });
    const listbox =
      listboxes.find((box) => box.getAttribute("id") === controlled) ??
      listboxes[0];
    await user.click(
      within(listbox).getByRole("option", { name: option, hidden: true }),
    );
  }

  it("keeps only complete movies when Complete is chosen", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickSubtitles(user, "Complete");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
  });

  it("keeps only movies still missing subtitles when Missing some is chosen", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickSubtitles(user, "Missing some");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
  });

  it("restores every movie when the Subtitles chip is removed", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickSubtitles(user, "Complete");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );

    await user.click(
      screen.getByRole("button", {
        name: "Remove filter: Subtitles: Complete",
      }),
    );

    await screen.findByRole("link", { name: "Northern Light" });
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
  });

  it("restores every movie when Clear all resets the Subtitles filter", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickSubtitles(user, "Complete");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );

    await user.click(within(band()).getByRole("button", { name: "Clear all" }));

    await screen.findByRole("link", { name: "Northern Light" });
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", {
        name: "Remove filter: Subtitles: Complete",
      }),
    ).toBeNull();
  });

  // The subtitles filter narrows on top of the other filters rather than
  // replacing them: Missing some AND a title search keep only the rows that
  // satisfy both, and the shown count reflects the intersection.
  it("ANDs the subtitles filter with the title search", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({
          data: [
            movie(1, "Glass Harbour", []),
            movie(2, "Northern Light", [frenchMissing]),
            movie(3, "Northern Star", [frenchMissing]),
          ],
          total: 3,
        }),
      ),
    );
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await openFilters(user);
    await pickSubtitles(user, "Missing some");
    await user.type(screen.getByPlaceholderText("Search by title..."), "star");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Northern Star" }),
    ).toBeInTheDocument();
    // Complete title dropped by the subtitles filter, not just the search.
    expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull();
    expect(shownCount()).toHaveTextContent(/^1 of 3 movies$/);
  });

  // A movie without a language profile has nothing to complete, so it shows
  // under Any but falls out of both Complete and Missing some.
  it("excludes a movie with no profile from both Complete and Missing some", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({
          data: [
            movie(1, "Glass Harbour", []),
            movie(2, "Northern Light", [frenchMissing]),
            movie(3, "Untracked Bay", [], null),
          ],
          total: 3,
        }),
      ),
    );
    customRender(<MovieView />);
    // Present under the default Any filter.
    await screen.findByRole("link", { name: "Untracked Bay" });

    await openFilters(user);
    await pickSubtitles(user, "Complete");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();

    await pickSubtitles(user, "Missing some");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
  });
});

// A scored movie carries lowest_subtitle_score (the backend fills it when the
// list is requested with scores=1). null means no current subtitle has a known
// score, so the row belongs to no narrowing group.
function scoredMovie(
  id: number,
  title: string,
  lowestScore: number | null,
): Item.Movie {
  return { ...movie(id, title, []), lowest_subtitle_score: lowestScore };
}

// The Score filter keeps rows whose lowest current-subtitle score matches the
// choice: Full keeps 100 %, Not full keeps anything short of it, Below threshold
// keeps anything under the configured acceptance threshold. A row with no known
// score belongs to none of them and shows only under Any. The "Lowest score"
// column appears only while the filter narrows.
describe("Movies score filter", () => {
  const full = scoredMovie(1, "Glass Harbour", 100);
  const mid = scoredMovie(2, "Northern Light", 85);
  const low = scoredMovie(3, "Southern Cross", 64);
  const unknown = scoredMovie(4, "Untracked Bay", null);

  beforeEach(() => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: { theme: "auto", minimum_score_movie: 70 },
        }),
      ),
      http.get("/api/movies", () =>
        HttpResponse.json({ data: [full, mid, low, unknown], total: 4 }),
      ),
    );
  });

  async function openFilters(user: ReturnType<typeof userEvent.setup>) {
    await user.click(
      await screen.findByRole("button", { name: "Toggle filters" }),
    );
  }

  // Same hidden-listbox dance as the subtitles select; the option is awaited so
  // the "Below threshold" label can pick up the live threshold first.
  async function pickScore(
    user: ReturnType<typeof userEvent.setup>,
    option: string,
  ) {
    const input = screen.getByRole("combobox", { name: "Score" });
    await user.click(input);
    if (input.getAttribute("aria-expanded") !== "true")
      await user.keyboard("{ArrowDown}");
    const controlled = input.getAttribute("aria-controls");
    const listboxes = await screen.findAllByRole("listbox", { hidden: true });
    const listbox =
      listboxes.find((box) => box.getAttribute("id") === controlled) ??
      listboxes[0];
    await user.click(
      await within(listbox).findByRole("option", {
        name: option,
        hidden: true,
      }),
    );
  }

  it("keeps only full-score movies when Full (100%) is chosen", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickScore(user, "Full (100%)");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Southern Cross" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull();
  });

  it("keeps only movies below 100 % when Not full (< 100%) is chosen", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickScore(user, "Not full (< 100%)");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Southern Cross" }),
    ).toBeInTheDocument();
    // The unknown-score row is not "below 100 %"; it belongs to no group.
    expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull();
  });

  it("keeps only movies under the configured threshold when Below threshold is chosen", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    // Threshold is the mocked minimum_score_movie (70).
    await pickScore(user, "Below threshold (< 70%)");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );
    // 64 < 70 stays; 85 and 100 drop.
    expect(
      screen.getByRole("link", { name: "Southern Cross" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull();
    expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull();
  });

  it("excludes a movie with no known score from every narrowing group", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    // Present under the default Any filter.
    await screen.findByRole("link", { name: "Untracked Bay" });

    await openFilters(user);
    for (const option of [
      "Full (100%)",
      "Not full (< 100%)",
      "Below threshold (< 70%)",
    ]) {
      await pickScore(user, option);
      await waitFor(() =>
        expect(
          screen.queryByRole("link", { name: "Untracked Bay" }),
        ).toBeNull(),
      );
    }
  });

  it("shows the Lowest score column with a pill per kept row only while the filter is active", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    // Hidden until the filter narrows.
    expect(
      screen.queryByRole("columnheader", { name: "Lowest score" }),
    ).toBeNull();

    await openFilters(user);
    await pickScore(user, "Not full (< 100%)");

    expect(
      await screen.findByRole("columnheader", { name: "Lowest score" }),
    ).toBeInTheDocument();
    // The kept rows carry a rounded pill; the dropped 100 % row takes its pill
    // with it. A null-score row is excluded while narrowing, so no dash shows.
    expect(screen.getByText("85%")).toBeInTheDocument();
    expect(screen.getByText("64%")).toBeInTheDocument();
    expect(screen.queryByText("100%")).toBeNull();
    expect(screen.queryByText("—")).toBeNull();
  });

  it("shows the full-band pill under Full and hides the column again under Any", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickScore(user, "Full (100%)");
    expect(
      await screen.findByRole("columnheader", { name: "Lowest score" }),
    ).toBeInTheDocument();
    expect(screen.getByText("100%")).toBeInTheDocument();

    await pickScore(user, "Any");
    await screen.findByRole("link", { name: "Untracked Bay" });
    expect(
      screen.queryByRole("columnheader", { name: "Lowest score" }),
    ).toBeNull();
  });

  it("restores every movie when the Score chip is removed", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickScore(user, "Full (100%)");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );

    await user.click(
      screen.getByRole("button", {
        name: "Remove filter: Score: Full (100%)",
      }),
    );

    await screen.findByRole("link", { name: "Northern Light" });
    expect(
      screen.getByRole("link", { name: "Southern Cross" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Untracked Bay" }),
    ).toBeInTheDocument();
    // Chip gone, so the column is gone with it.
    expect(
      screen.queryByRole("columnheader", { name: "Lowest score" }),
    ).toBeNull();
  });

  // Before the threshold setting loads (or when it is absent) scoreThreshold is
  // undefined, so the option is the plain "Below threshold" and ItemView's
  // graceful fallback keeps every scored row, dropping only null-score rows
  // rather than emptying the table.
  it("keeps scored rows and drops only null-score rows under Below threshold when the threshold is unknown", async () => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { theme: "auto" } }),
      ),
    );
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    await pickScore(user, "Below threshold");

    // The null-score row is dropped by every narrowing group.
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Untracked Bay" })).toBeNull(),
    );
    // Every scored row stays: with no threshold to compare against, none is
    // considered "below".
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Southern Cross" }),
    ).toBeInTheDocument();
  });
});
/* eslint-enable camelcase */
