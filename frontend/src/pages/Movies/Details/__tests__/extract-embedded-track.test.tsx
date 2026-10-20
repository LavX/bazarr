/* eslint-disable camelcase */

/**
 * Extract to file on a movie's embedded rows, and the tools that run on the
 * extracted file.
 *
 * A text track (or one whose codec is not known) offers every tool; the
 * backend extracts it first. An image-based track offers Extract to file only.
 * The request is the one the backend composes on: an empty path, the row's
 * language, HI and forced flags, its owner and, for Extract, its codec.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import Table from "@/pages/Movies/Details/table";
import { customRender, screen, waitFor } from "@/tests";
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

function embedded(codec: string | null): Subtitle {
  return {
    code2: "en",
    name: "English",
    hi: false,
    forced: false,
    path: null,
    codec,
  };
}

function mockPatch(answer: () => Response) {
  const requests: { action: string | null; form: Record<string, string> }[] =
    [];
  server.use(
    http.get("/api/subtitles/sync-status", () =>
      HttpResponse.json({ data: null }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.patch("/api/subtitles", async ({ request }) => {
      const form = Object.fromEntries(
        (await request.formData()).entries(),
      ) as Record<string, string>;
      requests.push({
        action: new URL(request.url).searchParams.get("action"),
        form,
      });
      return answer();
    }),
  );
  return requests;
}

async function openMenu(codec: string | null) {
  const user = userEvent.setup();
  customRender(<Table movie={movieWith([embedded(codec)])} history={[]} />);
  await screen.findByText("Video File Subtitle Track");
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  await user.click(await screen.findByLabelText("Subtitle Actions"));
  await screen.findByText("Remove HI Tags");
  return user;
}

// The dropdown can already be closing when the assertions run, so items are
// looked up whether or not they are still visible.
const item = (name: string) =>
  screen.getByRole("menuitem", { name, hidden: true });

describe("Movies Detail Table, Extract to file", () => {
  it("extracts a text track with its codec and names the file it writes", async () => {
    const requests = mockPatch(() =>
      HttpResponse.json(
        { job_id: 5, path: "/movies/test.en.srt" },
        { status: 202 },
      ),
    );
    const user = await openMenu("subrip");

    await user.click(item("Extract to file"));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].action).toBe("extract");
    expect(requests[0].form).toMatchObject({
      id: "7",
      type: "movie",
      language: "en",
      path: "",
      hi: "False",
      forced: "False",
      codec: "subrip",
      arr_instance_id: "2",
    });
    expect(await screen.findByText("Writing test.en.srt")).toBeInTheDocument();
  });

  it("says why when the file is already there", async () => {
    mockPatch(() =>
      HttpResponse.json(
        "A subtitle file named test.en.srt already exists. Delete it first to extract this track again.",
        { status: 409 },
      ),
    );
    const user = await openMenu("subrip");

    await user.click(item("Extract to file"));

    expect(await screen.findByText("Not extracted")).toBeInTheDocument();
    expect(
      screen.getByText(
        "A subtitle file named test.en.srt already exists. Delete it first to extract this track again.",
      ),
    ).toBeInTheDocument();
  });

  it("shows a tool's 409 once, from the Sync dialog too", async () => {
    const reason =
      "A subtitle file named test.en.srt already exists. Use the tool on that file's row instead.";
    const requests = mockPatch(() =>
      HttpResponse.json(reason, { status: 409 }),
    );
    server.use(
      http.get("/api/subtitles", () =>
        HttpResponse.json({
          data: {
            audio_tracks: [],
            embedded_subtitles_tracks: [],
            external_subtitles_tracks: [],
          },
        }),
      ),
    );
    const user = await openMenu("subrip");

    await user.click(item("Sync..."));
    await user.click(await screen.findByRole("button", { name: "Sync" }));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].action).toBe("sync");
    expect(await screen.findByText("Not started")).toBeInTheDocument();
    expect(screen.getByText(reason)).toBeInTheDocument();
    // The dialog stays open for another try, without a second, vaguer toast.
    expect(
      await screen.findByRole("button", { name: "Sync" }),
    ).not.toHaveAttribute("data-loading");
    expect(screen.queryByText("Sync failed")).not.toBeInTheDocument();
  });

  it("runs a tool on a text track through the embedded path", async () => {
    const requests = mockPatch(() =>
      HttpResponse.json({ job_id: 6, path: "" }, { status: 202 }),
    );
    const user = await openMenu(null);

    await user.click(item("Remove HI Tags"));

    await waitFor(() => expect(requests).toHaveLength(1));
    expect(requests[0].action).toBe("remove_HI");
    expect(requests[0].form).toMatchObject({
      id: "7",
      language: "en",
      path: "",
      arr_instance_id: "2",
    });
  });
});
