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

    const band = screen
      .getByPlaceholderText("Search by title...")
      .closest("[data-holds]");
    if (!(band instanceof HTMLElement)) throw new Error("No toolbar band");
    expect(within(band).getByRole("status")).toHaveTextContent(/^1 movie$/);
  });
});
