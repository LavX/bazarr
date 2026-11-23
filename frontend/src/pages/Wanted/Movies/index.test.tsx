/* eslint-disable camelcase */

import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { vi } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
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

  it("offers a tag that only a later page carries", async () => {
    const user = userEvent.setup();
    const untagged = Array.from({ length: 55 }, (_, index) => ({
      title: `Movie ${index + 1}`,
      id: 901 + index,
      radarrId: 1,
      missing_subtitles: [],
    }));
    const lateTagged = {
      title: "The Late Tag",
      id: 999,
      radarrId: 1,
      tags: ["Late Tag"],
      missing_subtitles: [],
    };
    const all = [...untagged, lateTagged];
    server.use(
      http.get("/api/movies/wanted", ({ request }) => {
        const { searchParams } = new URL(request.url);
        const length = Number(searchParams.get("length"));
        const start = Number(searchParams.get("start"));
        return HttpResponse.json({
          data: length === -1 ? all : all.slice(start, start + length),
          total: all.length,
        });
      }),
    );

    customRender(<WantedMoviesView />);
    await screen.findByRole("link", { name: "Movie 1" });
    expect(screen.queryByText("The Late Tag")).toBeNull();

    await user.click(screen.getByRole("button", { name: "Toggle filters" }));
    const listbox = await openSelect(user, "Filter by tags...");
    expect(
      within(listbox).getByRole("option", { name: "Late Tag", hidden: true }),
    ).toBeInTheDocument();
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

it("shows distinct Arr and folder rows and sends the selected source to the API", async () => {
  const user = userEvent.setup();
  const requests = vi.fn();
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { use_radarr: true },
        media_sources: { visible: true },
      }),
    ),
    http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([
        {
          id: 2,
          kind: "radarr",
          enabled: true,
          is_default: true,
          name: "Cinema Arr",
        },
      ]),
    ),
    http.get("/api/system/media-sources", () =>
      HttpResponse.json({
        data: [
          {
            id: 7,
            kind: "folder",
            name: "Cinema Folder",
            enabled: true,
            libraries: [{ content_kind: "movie", enabled: true }],
          },
        ],
      }),
    ),
    http.get("/api/movies/wanted", ({ request }) => {
      const params = new URL(request.url).searchParams;
      const source = params.get("source_key[]");
      requests(params.get("sources"), source);
      const rows = [
        {
          id: 9,
          origin: "arr",
          source_key: "arr:2",
          arr_instance_id: 2,
          radarrId: 123,
          title: "Arr title",
          missing_subtitles: [],
        },
        {
          id: 9,
          origin: "source",
          source_key: "source:7",
          source_id: 7,
          source_name: "Cinema Folder",
          radarrId: null,
          title: "Folder title",
          missing_subtitles: [],
        },
      ];
      const data = source
        ? rows.filter((row) => row.source_key === source)
        : rows;
      return HttpResponse.json({ data, total: data.length });
    }),
  );

  customRender(<WantedMoviesView />);
  expect(
    await screen.findByRole("link", { name: "Arr title" }),
  ).toHaveAttribute("href", "/movies/9");
  const folder = await screen.findByRole("row", { name: /Folder title/ });
  expect(within(folder).queryByRole("link")).not.toBeInTheDocument();
  expect(within(folder).getByText("Cinema Folder")).toBeInTheDocument();
  expect(requests).toHaveBeenCalledWith("1", null);

  await openSelect(user, "All sources");
  await user.click(
    await screen.findByRole("option", { name: "Cinema Folder" }),
  );
  await waitFor(() => expect(requests).toHaveBeenCalledWith("1", "source:7"));
  await waitFor(() =>
    expect(screen.queryByText("Arr title")).not.toBeInTheDocument(),
  );
});
