/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { customRender, screen, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";
import WantedMoviesView from ".";

describe("Wanted Movies", () => {
  it("should render with wanted movies", async () => {
    const mockMovies = [
      {
        title: "The Shawshank Redemption",
        id: 901,
        radarrId: 1,
        missing_subtitles: [
          {
            code2: "en",
            name: "English",
            hi: false,
            forced: false,
          },
        ],
      },
    ];

    server.use(
      http.get("/api/movies/wanted", () => {
        return HttpResponse.json({
          data: mockMovies,
        });
      }),
    );

    customRender(<WantedMoviesView />);

    const movieTitle = await screen.findByText("The Shawshank Redemption");
    expect(movieTitle).toBeInTheDocument();

    const movieLink = screen.getByRole("link", {
      name: "The Shawshank Redemption",
    });
    expect(movieLink).toHaveAttribute("href", "/movies/901");
  });

  it("should flag a movie with a release type mismatch", async () => {
    server.use(
      http.get("/api/movies/wanted", () => {
        return HttpResponse.json({
          data: [
            {
              title: "The Shawshank Redemption",
              id: 901,
              radarrId: 1,
              missing_subtitles: [],
              release_mismatch: true,
            },
            {
              title: "The Godfather",
              id: 902,
              radarrId: 2,
              missing_subtitles: [],
              release_mismatch: false,
            },
          ],
        });
      }),
    );

    customRender(<WantedMoviesView />);

    await screen.findByText("The Shawshank Redemption");
    expect(screen.getAllByText("Release mismatch")).toHaveLength(1);
  });

  it("should render empty state when no wanted movies", async () => {
    server.use(
      http.get("/api/movies/wanted", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );

    customRender(<WantedMoviesView />);

    const table = await screen.findByRole("table");
    expect(table).toBeInTheDocument();

    const movieTitle = screen.queryByText("The Shawshank Redemption");
    expect(movieTitle).not.toBeInTheDocument();
  });

  it("filters wanted movies by any selected Radarr tag", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/movies/wanted", () =>
        HttpResponse.json({
          data: [
            {
              title: "The Shawshank Redemption",
              id: 901,
              radarrId: 1,
              tags: ["Drama"],
              missing_subtitles: [],
            },
            {
              title: "The Godfather",
              id: 902,
              radarrId: 2,
              tags: ["Crime"],
              missing_subtitles: [],
            },
          ],
          total: 2,
        }),
      ),
    );

    customRender(<WantedMoviesView />);
    await screen.findByRole("link", { name: "The Godfather" });
    await user.click(screen.getByRole("button", { name: "Toggle filters" }));
    const listbox = await openSelect(user, "Filter by tags...");
    await user.click(
      within(listbox).getByRole("option", { name: "Drama", hidden: true }),
    );

    expect(
      screen.getByRole("link", { name: "The Shawshank Redemption" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "The Godfather" })).toBeNull();
    expect(screen.getByText("Tags: Drama")).toBeInTheDocument();
  });

  it("keeps the Wanted tags filter visible when no tags are available", async () => {
    const user = userEvent.setup();
    server.use(
      http.get("/api/movies/wanted", () =>
        HttpResponse.json({ data: [], total: 0 }),
      ),
    );
    customRender(<WantedMoviesView />);
    await screen.findByRole("table");
    await user.click(screen.getByRole("button", { name: "Toggle filters" }));

    expect(
      screen.getByPlaceholderText("No tags available"),
    ).toBeInTheDocument();
  });
});
