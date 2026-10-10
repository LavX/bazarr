import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";
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

  it("shows tag filtering when no tags are available", async () => {
    const user = userEvent.setup();
    customRender(<MovieView />);

    await user.click(
      await screen.findByRole("button", { name: "Toggle filters" }),
    );
    expect(await screen.findByText("Tags")).toBeInTheDocument();
    expect(screen.getByPlaceholderText("No tags available")).toBeInTheDocument();
  });

  it("counts one movie as a movie in the toolbar band", async () => {
    const movieItem = movie(7, "Glass Harbour", []);
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({ data: [movieItem], total: 1 }),
      ),
    );
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Glass Harbour" });

    const band = screen
      .getByPlaceholderText("Search by title...")
      .closest("[data-holds]");
    if (!(band instanceof HTMLElement)) throw new Error("No toolbar band");
    await waitFor(() =>
      expect(within(band).getByRole("status")).toHaveTextContent(/^1 movie$/),
    );
  });

  it("filters movies by any selected Radarr tag", async () => {
    const movies = [
      movie(1, "Northern Light", ["Drama"]),
      movie(2, "The Long Shore", ["Comedy"]),
      movie(3, "Glass Harbour", ["Drama", "Mystery"]),
    ];
    server.use(
      http.get("/api/movies", () =>
        HttpResponse.json({ data: movies, total: movies.length }),
      ),
    );
    const user = userEvent.setup();
    customRender(<MovieView />);
    await screen.findByRole("link", { name: "Northern Light" });

    await user.click(screen.getByRole("button", { name: "Toggle filters" }));
    const listbox = await openSelect(user, "Filter by tags...");
    await user.click(
      within(listbox).getByRole("option", { name: "Drama", hidden: true }),
    );

    expect(
      screen.getByRole("link", { name: "Northern Light" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Glass Harbour" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "The Long Shore" })).toBeNull();
    expect(screen.getByText("Tags: Drama")).toBeInTheDocument();
  });
});

function movie(id: number, title: string, tags: string[]): Item.Movie {
  return {
    id,
    radarrId: id,
    arr_instance_id: 2,
    title,
    path: `/movies/${title}`,
    tags,
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
}
