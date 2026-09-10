/* eslint-disable camelcase -- API fixture fields keep their transport names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it, vi } from "vitest";
import { COPIES_QUERY_KEY } from "@/apis/hooks/discover";
import queryClient from "@/apis/queries";
import { useDiscover } from "@/contexts/Discover";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { describeCopy } from "./LocalCopyPicker";
import {
  chooseSegment,
  findSelectInput,
  openSelect,
  pickOption,
  selectInput,
} from "./selectTestHelpers";
import Discover from ".";

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "copies-one",
  locale: "en-US",
  message: "Available",
  checked_at: null,
  fetched_at: null,
};
const show = {
  source: "tmdb",
  source_id: "tmdb:show:100",
  id: 100,
  media_type: "show",
  title: "Northern Light",
  year: 2020,
  imdb_id: "tt1234567",
  tvdb_id: 300,
  mapping_status: "resolved",
  overview: "A series with library copies.",
  poster_url: null,
  backdrop_url: null,
  seasons: [{ id: 201, season: 2, title: "Season 2", episode_count: 1 }],
};
const episode = {
  source: "tmdb",
  source_id: "tmdb:show:100:episode:401",
  show_id: 100,
  season_id: 201,
  id: 401,
  season: 2,
  episode: 1,
  title: "Home",
  air_date: "2026-09-01",
  imdb_id: "tt7654321",
  tvdb_id: 501,
  show_imdb_id: "tt1234567",
  show_tvdb_id: 300,
  show_title: "Northern Light",
  show_year: 2020,
  target_season: 2,
  target_episode: 1,
  numbering: "tvdb_default",
  identity_status: "resolved",
  absolute_episode: null,
  tvdb_absolute_number: null,
  mapping_updated_at: "2026-09-02",
};
const copyA = {
  copy_id: "c1.episode.3.1",
  media_type: "episode" as const,
  local_id: 3,
  arr_instance_id: 1,
  instance_name: "Sonarr HD",
  series_local_id: 30,
  title: "Northern Light",
  episode_title: "Home",
  release: "Northern.Light.S02E01.1080p.WEB.H264-GRP",
  filename: "northern.light.s02e01.mkv",
  source: "Web",
  resolution: "1080p",
  video_codec: "H.264",
  audio_codec: null,
  file_size: 2200000000,
  updated_at: "2026-09-02T09:00:00Z",
  selectable: true,
  unavailable_reason: null,
};
const copyB = {
  ...copyA,
  copy_id: "c1.episode.4.2",
  local_id: 4,
  arr_instance_id: 2,
  instance_name: "Sonarr 4K",
  series_local_id: 40,
  release: "Northern.Light.S02E01.2160p.BluRay.x265-OTHER",
  filename: "northern.light.s02e01.uhd.mkv",
  source: "Bluray",
  resolution: "2160p",
};

const summary = {
  generated_at: "2026-09-08T10:00:00Z",
  state: "quiet",
  query_budget: 4,
  activity: {
    availability: "available",
    observed_at: null,
    complete: true,
    truncated: false,
    running_count: 0,
    queued_count: 0,
    scheduled_count: 0,
    running: [],
    queued: [],
    scheduled: [],
    unknown_sources: [],
  },
  wanted: {
    availability: "available",
    observed_at: null,
    complete: true,
    requirements: 0,
    episode_requirements: 0,
    movie_requirements: 0,
    media_count: 0,
    unknown_media_count: 0,
    qualifications: [],
    by_instance: [],
  },
  arrivals: [],
  arrivals_status: {
    availability: "available",
    observed_at: null,
    complete: true,
    truncated: false,
    candidate_limit: 10,
    display_limit: 5,
    qualifications: [],
  },
  attention: {
    availability: "available",
    observed_at: null,
    complete: true,
    unknown_sources: [],
    items: [],
  },
  onboarding: {
    availability: "available",
    observed_at: null,
    complete: true,
    items: [],
  },
};

let searches: unknown[] = [];
let copyRequests: string[] = [];
let summaryReads = 0;
let copyPayload: {
  items: unknown[];
  truncated: boolean;
  owning_titles: number;
};

beforeEach(() => {
  localStorage.clear();
  searches = [];
  copyRequests = [];
  summaryReads = 0;
  copyPayload = { items: [copyA, copyB], truncated: false, owning_titles: 2 };
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        discover: {
          tmdb_configured: true,
          metadata_revision: "copies-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code3: "eng", code2: "en", enabled: false },
      ]),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/metadata/shows/100", () =>
      HttpResponse.json({ data: { ...envelope, item: show } }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2", () =>
      HttpResponse.json({
        data: {
          ...envelope,
          season: { id: 201, season: 2, episodes: [episode] },
        },
      }),
    ),
    http.get("/api/discover/metadata/shows/100/seasons/2/episodes/1", () =>
      HttpResponse.json({ data: { ...envelope, episode } }),
    ),
    http.get("/api/discover/summary", () => {
      summaryReads += 1;
      return HttpResponse.json(summary);
    }),
    http.get("/api/discover/copies", ({ request }) => {
      copyRequests.push(new URL(request.url).search);
      return HttpResponse.json(copyPayload);
    }),
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return new HttpResponse(null, { status: 503 });
    }),
  );
});

function Appearance() {
  const { toggleColorScheme } = useMantineColorScheme();
  return <Button onClick={() => toggleColorScheme()}>Change appearance</Button>;
}

/** Reads the two restoration slots the page owns, so a test can tell them apart. */
function Slots() {
  const { state } = useDiscover();
  return (
    <output data-testid="slots">
      {JSON.stringify({
        focusId: state.browsing.focusId,
        pagePosition: state.browsing.pagePosition,
      })}
    </output>
  );
}

function browse(initial = "/discover?show=100&season=2&episode=1") {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <>
            <Appearance />
            <Slots />
            <Discover />
          </>
        ),
      },
      { path: "/series/:id", element: <div>Series library page</div> },
      { path: "/movies/:id", element: <div>Movie library page</div> },
    ],
    { initialEntries: [initial] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { router, user: userEvent.setup() };
}

async function ready() {
  return findSelectInput("Library copy");
}

it("defaults an exact episode to title-only matching even when copies exist", async () => {
  const { user } = browse();
  const select = await ready();
  // Title-only is the default, and the control says so in words. A blank field
  // here would mean the reader is looking at a choice with no name.
  expect(select).toHaveValue("Title only, no library copy");
  // The copies are offered, and none is chosen for the reader.
  const listbox = await openSelect(user, "Library copy");
  expect(within(listbox).getAllByRole("option", { hidden: true })).toBeTruthy();
  await user.keyboard("{Escape}");
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).not.toHaveProperty("copy_id");
  expect(copyRequests[0]).toContain("media_type=episode");
  expect(copyRequests[0]).toContain("season=2");
  expect(copyRequests[0]).toContain("episode=1");
});

it("offers every exact copy with its owning instance, local identity and release", async () => {
  const { user } = browse();
  await ready();
  const listbox = await openSelect(user, "Library copy");
  expect(
    within(listbox).getByRole("option", {
      name: /Sonarr HD.*episode 3.*Northern\.Light\.S02E01\.1080p\.WEB\.H264-GRP/,
      hidden: true,
    }),
  ).toBeInTheDocument();
  await user.click(
    within(listbox).getByRole("option", {
      name: describeCopy(copyB),
      hidden: true,
    }),
  );
  // The summary adds the file behind the release rather than repeating the
  // option label the reader just chose.
  expect(await screen.findByText(/Chosen file:/)).toHaveTextContent(
    /northern\.light\.s02e01\.uhd\.mkv.*2\.2 GB.*Library record updated/,
  );
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({ copy_id: copyB.copy_id });
});

it("routes to the exact library item without offering any save or import", async () => {
  const { user } = browse();
  await pickOption(user, "Library copy", describeCopy(copyA));
  const link = await screen.findByRole("link", {
    name: "Open this item in your library",
  });
  expect(link).toHaveAttribute("href", "/series/30");
  expect(
    screen.queryByRole("button", { name: /save|import|add to library/i }),
  ).toBeNull();
  await user.click(link);
  expect(await screen.findByText("Series library page")).toBeInTheDocument();
});

it("states that a library copy is search context and never a timing guarantee", async () => {
  browse();
  await ready();
  expect(
    screen.getByText(
      /does not verify subtitle timing, and nothing is saved to your library/i,
    ),
  ).toBeInTheDocument();
});

it("explains an absent episode separately from an unowned title", async () => {
  copyPayload = { items: [], truncated: false, owning_titles: 2 };
  browse();
  expect(
    await screen.findByText(
      /2 series in your library match this title, but none holds this episode/i,
    ),
  ).toBeInTheDocument();
});

it("keeps a chosen copy through appearance and local status changes", async () => {
  const { user } = browse();
  await pickOption(user, "Library copy", describeCopy(copyA));
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));

  // Changing appearance is not a change of search context.
  await user.click(screen.getByRole("button", { name: "Change appearance" }));
  expect(selectInput("Library copy")).toHaveValue(describeCopy(copyA));

  // Neither is inspecting this Bazarr's own work, or retrying that read.
  await user.click(
    await screen.findByRole("button", { name: "Refresh local status" }),
  );
  await waitFor(() => expect(summaryReads).toBeGreaterThan(1));
  expect(selectInput("Library copy")).toHaveValue(describeCopy(copyA));

  server.use(
    http.get("/api/discover/summary", () => {
      summaryReads += 1;
      return new HttpResponse(null, { status: 503 });
    }),
  );
  await user.click(
    screen.getByRole("button", { name: "Refresh local status" }),
  );
  await screen.findByRole("button", { name: "Retry local status" });
  expect(selectInput("Library copy")).toHaveValue(describeCopy(copyA));

  // None of it searched a provider or re-read the offer under a new key.
  expect(searches).toHaveLength(1);
  expect(copyRequests).toHaveLength(1);
});

it("requires an explicit recovery choice when the chosen copy disappears", async () => {
  const { user } = browse();
  await pickOption(user, "Library copy", describeCopy(copyA));
  await pickOption(user, "Subtitle language", "English");
  server.use(
    http.post("/api/discover/search", async ({ request }) => {
      searches.push(await request.json());
      return HttpResponse.json(
        {
          message:
            "This library copy is no longer available. Choose another copy or search the title only.",
          reason: "copy_unavailable",
          recoverable: true,
        },
        { status: 409 },
      );
    }),
  );
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  expect(
    await screen.findByText(/This library copy is no longer available/),
  ).toBeInTheDocument();
  // Never substitute another copy on the reader's behalf.
  expect(selectInput("Library copy")).toHaveValue(describeCopy(copyA));
  expect(searches).toHaveLength(1);
});

it("marks an unselectable copy and refuses to choose it", async () => {
  copyPayload = {
    items: [
      {
        ...copyA,
        selectable: false,
        unavailable_reason: "owner_unknown",
        instance_name: null,
        arr_instance_id: null,
      },
    ],
    truncated: false,
    owning_titles: 1,
  };
  const { user } = browse();
  await ready();
  const listbox = await openSelect(user, "Library copy");
  const option = within(listbox).getByRole("option", {
    name: /Owner unknown/,
    hidden: true,
  });
  expect(option).toHaveAttribute("data-combobox-disabled");
});

it("changing the target retires the chosen copy instead of carrying it over", async () => {
  const { user } = browse();
  await pickOption(user, "Library copy", describeCopy(copyA));
  await pickOption(user, "Subtitle language", "English");
  // The same series IMDb id, now as a film. That is a different target, so the
  // episode copy chosen for it cannot travel with the search.
  await chooseSegment(user, "Movie");
  // The picker follows the reader to the manual form and re-offers copies for
  // the new target, but the episode copy chosen for the old one is gone.
  await waitFor(() =>
    expect(selectInput("Library copy")).toHaveValue(
      "Title only, no library copy",
    ),
  );
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({ media_type: "movie" });
  expect(searches[0]).not.toHaveProperty("copy_id");
  expect(copyRequests.at(-1)).toContain("media_type=movie");
});

it("does not read library copies before a target is confirmed", async () => {
  const focus = vi.spyOn(HTMLElement.prototype, "focus");
  browse("/discover?show=100");
  await screen.findByRole("heading", { name: "Northern Light" });
  await waitFor(() =>
    expect(screen.queryAllByLabelText("Library copy")[0] ?? null).toBeNull(),
  );
  expect(copyRequests).toEqual([]);
  focus.mockRestore();
});

const movieCopy = {
  copy_id: "c1.movie.5.1",
  media_type: "movie" as const,
  local_id: 5,
  arr_instance_id: 1,
  instance_name: "Radarr HD",
  series_local_id: null,
  title: "The Matrix",
  episode_title: null,
  release: "The.Matrix.1999.1080p.WEB.H264-GRP",
  filename: "the.matrix.1999.mkv",
  source: "Web",
  resolution: "1080p",
  video_codec: "H.264",
  audio_codec: null,
  file_size: 8_400_000_000,
  updated_at: "2026-09-02T09:00:00Z",
  selectable: true,
  unavailable_reason: null,
};

it("matches a copy for an IMDb identifier typed straight into the form", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  // No title was browsed to. This is the manual retrieval path, and it has to
  // offer exactly the same explicit copy matching as the browsing path.
  expect(screen.queryAllByLabelText("Library copy")[0] ?? null).toBeNull();
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  const select = await findSelectInput("Library copy");
  expect(select).toHaveValue("Title only, no library copy");
  expect(copyRequests[0]).toContain("media_type=movie");
  expect(copyRequests[0]).toContain("imdb_id=tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));
  expect(
    await screen.findByRole("link", { name: "Open this item in your library" }),
  ).toHaveAttribute("href", "/movies/5");
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({
    media_type: "movie",
    imdb_id: "tt0133093",
    copy_id: movieCopy.copy_id,
  });
});

it("keeps exactly one picker when a browsed title is open", async () => {
  const { user } = browse();
  await ready();
  expect(screen.getAllByLabelText("Library copy")).toHaveLength(1);
  // Leaving the selected title returns the reader to the manual form, which
  // owns the only picker on that branch.
  await user.click(screen.getByRole("button", { name: /^Back to/ }));
  await waitFor(() =>
    expect(screen.getAllByLabelText("Library copy")).toHaveLength(1),
  );
});

it("offers no copy matching for an unverified release-name query", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));
  await user.click(
    screen.getByRole("button", { name: "Search providers by release name" }),
  );
  await waitFor(() =>
    expect(screen.queryAllByLabelText("Library copy")[0] ?? null).toBeNull(),
  );
  await user.type(screen.getByLabelText("Release name"), "Some.Release.1080p");
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  // A release query can never carry a copy.
  expect(searches[0]).not.toHaveProperty("copy_id");
  // Looking at release search and coming back does not discard the choice.
  await user.click(
    screen.getByRole("button", { name: "Return to identified title" }),
  );
  expect(await ready()).toHaveValue(describeCopy(movieCopy));
});

it("stops claiming a copy is gone when the offer simply could not be read", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));
  // One failed background refetch, with the target unchanged. The query does
  // not retry, so this is all it takes to reach the state.
  server.use(
    http.get(
      "/api/discover/copies",
      () => new HttpResponse(null, { status: 503 }),
    ),
  );
  await queryClient.invalidateQueries({ queryKey: COPIES_QUERY_KEY });
  expect(
    await screen.findByText(/Your library copies could not be read/),
  ).toBeInTheDocument();
  // The page must not assert something false about the reader's library.
  expect(screen.queryByText(/no longer offered/)).toBeNull();
  expect(selectInput("Library copy")).toHaveValue(describeCopy(movieCopy));
  // And the choice stays clearable while the list is unreadable.
  await user.click(
    screen.getByRole("button", { name: "Search the title only" }),
  );
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).not.toHaveProperty("copy_id");
});

it("records its position in the slot the branch it renders on actually uses", async () => {
  const { user } = browse();
  const slots = () =>
    JSON.parse(screen.getByTestId("slots").textContent ?? "{}") as {
      focusId: string;
      pagePosition: { target: string; focusId: string } | null;
    };
  const before = slots();
  await pickOption(user, "Library copy", describeCopy(copyA));
  const after = slots();
  // A selected-title page stores a target-keyed position. Writing the picker's
  // own offset into the homepage slot would put a title-page offset under the
  // homepage's key, which is what the page's own listeners already avoid.
  expect(after.pagePosition?.focusId).toBe("discover-local-copy");
  expect(after.focusId).toBe(before.focusId);
  expect(after.focusId).not.toBe("discover-local-copy");
});

it("records the homepage slot when it renders inside the retrieval form", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));
  const slots = JSON.parse(screen.getByTestId("slots").textContent ?? "{}") as {
    focusId: string;
    pagePosition: unknown;
  };
  expect(slots.focusId).toBe("discover-local-copy");
  expect(slots.pagePosition).toBeNull();
});

it("keeps the control and the choice in step after a retirement and a failed refetch", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));

  // A genuine retirement: a successful read that no longer lists the copy.
  copyPayload = { items: [], truncated: false, owning_titles: 1 };
  await queryClient.invalidateQueries({ queryKey: COPIES_QUERY_KEY });
  expect(
    await screen.findByText(/The copy you chose is no longer offered/),
  ).toBeInTheDocument();
  // The control names the retired choice rather than going blank or
  // pretending the copy is still offered.
  expect(selectInput("Library copy")).toHaveValue(
    "Previously chosen copy, no longer offered",
  );

  // Then a failed refetch. The control must not go blank while the draft still
  // holds the copy, because the next search would send an id the reader can no
  // longer see.
  server.use(
    http.get(
      "/api/discover/copies",
      () => new HttpResponse(null, { status: 503 }),
    ),
  );
  await queryClient.invalidateQueries({ queryKey: COPIES_QUERY_KEY });
  expect(
    await screen.findByText(/Your library copies could not be read/),
  ).toBeInTheDocument();
  expect(selectInput("Library copy")).toHaveValue(
    "Previously chosen copy, no longer offered",
  );
  // What the control shows and what the next search sends still agree.
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({ copy_id: movieCopy.copy_id });
});

it("never labels an unchecked copy as retired", async () => {
  // The offer fails on its very first read, so nothing successful was ever
  // observed for this target and no retirement can be claimed.
  server.use(
    http.get(
      "/api/discover/copies",
      () => new HttpResponse(null, { status: 503 }),
    ),
  );
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  expect(
    await screen.findByText(/Your library copies could not be read/),
  ).toBeInTheDocument();
  expect(
    screen.queryByText(/The copy you chose is no longer offered/),
  ).toBeNull();
  expect(screen.queryAllByLabelText("Library copy")[0] ?? null).toBeNull();
});

it("does not call a copy retired when the list it is missing from is truncated", async () => {
  copyPayload = { items: [movieCopy], truncated: false, owning_titles: 1 };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));

  // The same target, now answered with a capped list that no longer shows the
  // chosen copy. Absence from a truncated list is evidence of nothing: the
  // copy can sit past the cap and still resolve.
  copyPayload = { items: [], truncated: true, owning_titles: 1 };
  await queryClient.invalidateQueries({ queryKey: COPIES_QUERY_KEY });
  expect(
    await screen.findByText(
      /Your library holds more copies of this film than are listed here/i,
    ),
  ).toBeInTheDocument();
  // The generic truncation note says the same thing in different words, so it
  // is suppressed here. Its own wording is asserted, not the alert's, because
  // a regex that cannot match it would let the guard be removed silently.
  expect(
    screen.queryByText(/More copies of this film exist than are listed here/i),
  ).toBeNull();
  expect(
    screen.queryByText(/The copy you chose is no longer offered/),
  ).toBeNull();
  // The control says the copy sits beyond the list rather than calling it
  // retired or going blank.
  expect(selectInput("Library copy")).toHaveValue(
    "Previously chosen copy, beyond this list",
  );
  // The server still decides, so the choice is sent.
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  expect(searches[0]).toMatchObject({ copy_id: movieCopy.copy_id });
});

it("labels an unrecognised unavailable reason without borrowing another one", async () => {
  copyPayload = {
    items: [
      {
        ...movieCopy,
        selectable: false,
        unavailable_reason: "something_new_from_the_server",
      },
    ],
    truncated: false,
    owning_titles: 1,
  };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await ready();
  const listbox = await openSelect(user, "Library copy");
  expect(
    within(listbox).getByRole("option", {
      name: /It cannot be used for this search/,
      hidden: true,
    }),
  ).toHaveAttribute("data-combobox-disabled");
  expect(screen.queryByText(/Its owning instance is unknown/)).toBeNull();
});

it("reports a recorded size honestly at both ends of the range", async () => {
  copyPayload = {
    items: [{ ...movieCopy, file_size: 0 }],
    truncated: false,
    owning_titles: 1,
  };
  const { user } = browse("/discover");
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Library copy", describeCopy(movieCopy));
  // A zero-byte library record is odd, and inventing a megabyte for it would
  // be the control asserting something the row does not say.
  expect(await screen.findByText(/Chosen file:/)).toHaveTextContent("0 bytes");
});

// Title-only used to carry the empty string as its value, which a select reads
// as nothing chosen: the option existed in the list but could never be the
// value on screen, so the default state and the way back to it were both blank.
it("shows title-only as the value it is, and returns to it by name", async () => {
  const { user } = browse();
  const select = await ready();
  expect(select).toHaveValue("Title only, no library copy");
  const listbox = await openSelect(user, "Library copy");
  expect(
    within(listbox).getByRole("option", {
      name: "Title only, no library copy",
      hidden: true,
    }),
  ).toBeInTheDocument();
  await user.keyboard("{Escape}");
  await pickOption(user, "Library copy", describeCopy(copyA));
  expect(selectInput("Library copy")).toHaveValue(describeCopy(copyA));
  await pickOption(user, "Library copy", "Title only, no library copy");
  expect(selectInput("Library copy")).toHaveValue(
    "Title only, no library copy",
  );
  await pickOption(user, "Subtitle language", "English");
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(searches).toHaveLength(1));
  // Named or not, the choice still means no copy is sent.
  expect(searches[0]).not.toHaveProperty("copy_id");
});
