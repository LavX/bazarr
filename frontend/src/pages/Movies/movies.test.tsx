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
/* eslint-enable camelcase */
