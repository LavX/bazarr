/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SeriesView from ".";

describe("Series page", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/series", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );
  });

  it("should render", async () => {
    customRender(<SeriesView />);

    await waitFor(() => {
      expect(
        screen.getByPlaceholderText("Search by title..."),
      ).toBeInTheDocument();
    });
  });
});

function series(id: number, title: string): Item.Series {
  return {
    id,
    sonarrSeriesId: id,
    arr_instance_id: 1,
    title,
    path: `/tv/${title}`,
    tags: [],
    monitored: true,
    audio_language: [{ code2: "en", name: "English" }],
    profileId: null,
    fanart: "",
    overview: "",
    imdbId: "",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    episodeFileCount: 10,
    episodeMissingCount: 0,
    ended: false,
    lastAired: "2026-09-01",
    seriesType: "Standard",
    tvdbId: 1000 + id,
  };
}

// The band above the table used to be two groups with the left one empty
// until something was selected, and the active filters rendered in a second
// band under it. jsdom cannot measure either band, so these pin the condition
// that decides what the left side holds, which is what a layout change would
// have to get wrong first.
describe("Series toolbar band", () => {
  const rows = [
    series(1, "Northern Light"),
    series(2, "The Long Shore"),
    series(3, "Glass Harbour"),
  ];

  beforeEach(() => {
    server.use(
      http.get("/api/series", () =>
        HttpResponse.json({ data: rows, total: rows.length }),
      ),
    );
  });

  // The band has no role of its own, so it is found as the search field's
  // container: the same element the layout keys on.
  function band() {
    const search = screen.getByPlaceholderText("Search by title...");
    // eslint-disable-next-line testing-library/no-node-access
    const found = search.closest("[data-holds]");
    if (!(found instanceof HTMLElement)) throw new Error("No toolbar band");
    return found;
  }

  // The count a sighted reader sees. It is hidden from assistive technology,
  // which hears the same words from the status region once typing settles,
  // so it is told apart from that region by its aria-hidden.
  function shownCount() {
    return (
      within(band())
        .queryAllByText(/series$/)
        .find((element) => element.getAttribute("aria-hidden") === "true") ??
      null
    );
  }

  async function announced(text: RegExp) {
    await waitFor(() =>
      expect(within(band()).getByRole("status")).toHaveTextContent(text),
    );
  }

  it("leads with the count and renders no tool group while nothing is selected", async () => {
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    expect(band()).toHaveAttribute("data-holds", "summary");
    expect(shownCount()).toHaveTextContent(/^3 series$/);
    await announced(/^3 series$/);
    // Nothing selected is no group at all, not an empty one holding the space.
    expect(screen.queryByRole("group", { name: "Batch actions" })).toBeNull();
    expect(
      screen.queryByRole("button", { name: /sync subtitles/i }),
    ).toBeNull();
  });

  it("swaps the count for the batch tools while rows are selected", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await user.click(
      screen.getByRole("checkbox", { name: "Select Northern Light" }),
    );
    expect(band()).toHaveAttribute("data-holds", "actions");
    const tools = within(band()).getByRole("group", { name: "Batch actions" });
    // The batch tools are the reason the band exists: a layout that hid the
    // band with an empty left side must never take these with it.
    for (const name of [/sync subtitles/i, /translate/i, /combine/i]) {
      expect(within(tools).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(shownCount()).toBeNull();

    await user.click(
      screen.getByRole("checkbox", { name: "Select Northern Light" }),
    );
    expect(band()).toHaveAttribute("data-holds", "summary");
    expect(screen.queryByRole("group", { name: "Batch actions" })).toBeNull();
    expect(shownCount()).toHaveTextContent(/^3 series$/);
  });

  it("keeps the active filters in the band instead of a second one under it", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await user.type(screen.getByPlaceholderText("Search by title..."), "light");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull(),
    );
    expect(shownCount()).toHaveTextContent(/^1 of 3 series$/);
    await announced(/^1 of 3 series$/);
    expect(within(band()).getByText("Active filters:")).toBeInTheDocument();
    expect(within(band()).getByText('Title: "light"')).toBeInTheDocument();
    // Once, and in the band: no second band carrying its own copy.
    expect(screen.getAllByText("Active filters:")).toHaveLength(1);

    await user.click(within(band()).getByRole("button", { name: "Clear all" }));
    expect(screen.getByPlaceholderText("Search by title...")).toHaveValue("");
    expect(shownCount()).toHaveTextContent(/^3 series$/);
    expect(screen.queryByText("Active filters:")).toBeNull();
  });

  // Selecting a row changes what the head says and nothing else. If the
  // filters left with the count, the band would lose a row on the first
  // selection, and the table would move under the pointer while the only
  // sign that it is filtered went with them.
  it("keeps the active filters while rows are selected", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await user.type(screen.getByPlaceholderText("Search by title..."), "light");
    await screen.findByText('Title: "light"');
    await user.click(
      screen.getByRole("checkbox", { name: "Select Northern Light" }),
    );
    expect(band()).toHaveAttribute("data-holds", "actions");
    expect(within(band()).getByText('Title: "light"')).toBeInTheDocument();
    expect(
      within(band()).getByRole("button", { name: "Clear all" }),
    ).toBeInTheDocument();
  });
});

function seriesWith(
  id: number,
  title: string,
  episodeFileCount: number,
  episodeMissingCount: number,
): Item.Series {
  return {
    ...series(id, title),
    episodeFileCount,
    episodeMissingCount,
  };
}

// The Subtitles filter keeps rows whose completeness matches the choice. A
// series is complete when no episode is missing; one with no episode files has
// nothing that can be missing, so it counts as complete too.
describe("Series subtitles filter", () => {
  const complete = seriesWith(1, "Northern Light", 10, 0);
  const missing = seriesWith(2, "The Long Shore", 10, 3);
  const empty = seriesWith(3, "Glass Harbour", 0, 0);

  beforeEach(() => {
    server.use(
      http.get("/api/series", () =>
        HttpResponse.json({
          data: [complete, missing, empty],
          total: 3,
        }),
      ),
    );
  });

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

  it("keeps only complete series when Complete is chosen", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "The Long Shore" });

    await openFilters(user);
    await pickSubtitles(user, "Complete");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "The Long Shore" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
  });

  it("keeps only series still missing episodes when Missing some is chosen", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await openFilters(user);
    await pickSubtitles(user, "Missing some");

    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "The Long Shore" }),
    ).toBeInTheDocument();
  });

  it("counts a series with no episode files as complete", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    await openFilters(user);
    // Nothing can be missing, so it stays under Complete...
    await pickSubtitles(user, "Complete");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "The Long Shore" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();

    // ...and drops out under Missing some.
    await pickSubtitles(user, "Missing some");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Glass Harbour" })).toBeNull(),
    );
    expect(
      screen.getByRole("link", { name: "The Long Shore" }),
    ).toBeInTheDocument();
  });

  it("restores every series when the Subtitles chip is removed", async () => {
    const user = userEvent.setup();
    customRender(<SeriesView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await openFilters(user);
    await pickSubtitles(user, "Missing some");
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: "Northern Light" })).toBeNull(),
    );

    await user.click(
      screen.getByRole("button", {
        name: "Remove filter: Subtitles: Missing some",
      }),
    );

    await screen.findByRole("link", { name: "Northern Light" });
    expect(
      screen.getByRole("link", { name: "The Long Shore" }),
    ).toBeInTheDocument();
  });
});
