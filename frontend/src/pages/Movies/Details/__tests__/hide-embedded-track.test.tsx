/* eslint-disable camelcase */

/**
 * Hide track on a movie's embedded row, and the "Hidden tracks (n)"
 * disclosure under the table that undoes it.
 *
 * The movie's local id and its radarrId differ on purpose: hidden tracks are
 * recorded against the local id, and sending the upstream one would hide a
 * track on whichever movie happens to hold that local id.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

function movieWith(subtitles: Subtitle[]): Item.Movie {
  return {
    id: 12,
    radarrId: 7,
    arr_instance_id: 2,
    title: "Test Movie",
    path: "/movies/test.mkv",
    profileId: 1,
    fanart: "",
    overview: "",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    monitored: true,
    tags: [],
    audio_language: [],
    subtitles,
    missing_subtitles: [],
  };
}

const embeddedEnglish: Subtitle = {
  code2: "en",
  name: "English",
  hi: true,
  forced: false,
  path: null,
};

// What the backend answers when the hidden track's language is now wanted.
function wantedAgain(form: Record<string, string>) {
  return HttpResponse.json({
    data: {
      wanted: true,
      hidden: {
        code2: form.language,
        name: form.language,
        hi: form.hi === "True",
        forced: form.forced === "True",
      },
      covered_by: [],
    },
  });
}

function mockCommon(
  hidden: unknown[] = [],
  answer: (form: Record<string, string>) => Response = wantedAgain,
) {
  const requests: {
    method: string;
    form?: Record<string, string>;
    url: string;
  }[] = [];
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/subtitles/embedded-hidden", ({ request }) => {
      requests.push({ method: "GET", url: request.url });
      return HttpResponse.json({ data: hidden });
    }),
    http.post("/api/subtitles/embedded-hidden", async ({ request }) => {
      const form = Object.fromEntries(
        (await request.formData()).entries(),
      ) as Record<string, string>;
      requests.push({ method: "POST", form, url: request.url });
      return answer(form);
    }),
    http.delete("/api/subtitles/embedded-hidden", async ({ request }) => {
      const form = Object.fromEntries(
        (await request.formData()).entries(),
      ) as Record<string, string>;
      requests.push({ method: "DELETE", form, url: request.url });
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return requests;
}

describe("Movies Detail Table, Hide track", () => {
  it("hides an embedded track by the movie's local id", async () => {
    const requests = mockCommon();
    const user = userEvent.setup();
    customRender(<Table movie={movieWith([embeddedEnglish])} history={[]} />);
    // Let the page's queries settle first, so the click lands on the menu
    // the final render shows.
    await screen.findByText("Video File Subtitle Track");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    await user.click(await screen.findByLabelText("Subtitle Actions"));
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
    );
    expect(
      dialog.getByText("English (HI) (embedded track)"),
    ).toBeInTheDocument();
    await user.click(dialog.getByRole("button", { name: "Hide track" }));

    await waitFor(() =>
      expect(requests.find((r) => r.method === "POST")?.form).toEqual({
        media_type: "movie",
        media_id: "12",
        arr_instance_id: "2",
        language: "en",
        hi: "True",
        forced: "False",
      }),
    );
  });

  it("says so when another subtitle still covers the language", async () => {
    mockCommon([], () =>
      HttpResponse.json({
        data: {
          wanted: false,
          hidden: { code2: "en", name: "English", hi: true, forced: false },
          covered_by: [
            {
              code2: "en",
              name: "English",
              hi: false,
              forced: false,
              embedded: false,
            },
          ],
        },
      }),
    );
    const user = userEvent.setup();
    customRender(<Table movie={movieWith([embeddedEnglish])} history={[]} />);
    await screen.findByText("Video File Subtitle Track");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    await user.click(await screen.findByLabelText("Subtitle Actions"));
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    await user.click(
      within(
        await screen.findByRole("dialog", {
          name: "Hide this embedded track?",
        }),
      ).getByRole("button", { name: "Hide track" }),
    );

    expect(
      await screen.findByText(
        "English (HI) is still covered by English (subtitle file), so Bazarr+ will not search for it.",
      ),
    ).toBeInTheDocument();
  });

  it("shows why the backend refused the hide", async () => {
    const requests = mockCommon([], () =>
      HttpResponse.json(
        "The video file cannot be read right now, so the track was not hidden.",
        { status: 409 },
      ),
    );
    const user = userEvent.setup();
    customRender(<Table movie={movieWith([embeddedEnglish])} history={[]} />);
    await screen.findByText("Video File Subtitle Track");
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));

    await user.click(await screen.findByLabelText("Subtitle Actions"));
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    const listings = () => requests.filter((r) => r.method === "GET").length;
    const before = listings();
    await user.click(
      within(
        await screen.findByRole("dialog", {
          name: "Hide this embedded track?",
        }),
      ).getByRole("button", { name: "Hide track" }),
    );

    expect(await screen.findByText("Track not hidden")).toBeInTheDocument();
    expect(
      screen.getByText(
        "The video file cannot be read right now, so the track was not hidden.",
      ),
    ).toBeInTheDocument();
    // A hide that failed part way may still have changed the row, so the page
    // reads it again after an error as well.
    await waitFor(() => expect(listings()).toBeGreaterThan(before));
  });

  it("lists hidden tracks under the table and unhides one", async () => {
    const requests = mockCommon([
      {
        media_type: "movie",
        media_id: 12,
        arr_instance_id: 2,
        language: "fr",
        code2: "fr",
        name: "French",
        hi: false,
        forced: false,
        codec: "pgs",
        hidden_at: "2026-09-26T10:00:00",
      },
    ]);
    const user = userEvent.setup();
    customRender(<Table movie={movieWith([embeddedEnglish])} history={[]} />);

    const disclosure = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    await waitFor(() => expect(queryClient.isFetching()).toBe(0));
    const listed = requests.find((r) => r.method === "GET");
    expect(listed?.url).toContain("media_type=movie");
    expect(listed?.url).toContain("media_id%5B%5D=12");
    await user.click(disclosure);
    await user.click(
      await screen.findByRole("button", { name: "Unhide French" }),
    );

    await waitFor(() =>
      expect(requests.find((r) => r.method === "DELETE")?.form).toEqual({
        media_type: "movie",
        media_id: "12",
        arr_instance_id: "2",
        language: "fr",
        hi: "False",
        forced: "False",
      }),
    );
  });

  it("shows no disclosure when nothing is hidden", async () => {
    mockCommon();
    customRender(<Table movie={movieWith([embeddedEnglish])} history={[]} />);
    await screen.findByText("Video File Subtitle Track");
    expect(
      screen.queryByRole("button", { name: /Hidden tracks/ }),
    ).not.toBeInTheDocument();
  });
});
