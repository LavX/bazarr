/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, RouterProvider } from "react-router";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { pickOption, selectInput } from "./selectTestHelpers";
import Discover from "./testHarness";

// The global fixture serves an empty profile list, so every other Discover test
// exercises a reader who has no language profile at all. That is not the normal
// install, and it means the seeding branch runs zero times in the rest of the
// suite: the tests that look like they cover it pass because the fixture is
// empty, not because the gate is right. These serve real profiles.

const languages = [
  { code2: "hu", code3: "hun", name: "Hungarian", enabled: true },
  { code2: "en", code3: "eng", name: "English", enabled: true },
  { code2: "de", code3: "deu", name: "German", enabled: true },
];

const movie = {
  source: "tmdb",
  source_id: "tmdb:movie:42",
  id: 42,
  media_type: "movie",
  title: "Northern Light",
  year: 2008,
  imdb_id: "tt0080274",
  mapping_status: "resolved",
  overview: "A journey north.",
  poster_url: null,
  backdrop_url: null,
};

const envelope = {
  source: "tmdb",
  status: "available",
  configured: true,
  revision: "feed-one",
  locale: "en-US",
};

function profile(id: number, code2: string, name: string) {
  return {
    profileId: id,
    name,
    cutoff: null,
    items: [
      {
        id: 1,
        language: code2,
        audio_exclude: "False",
        hi: "False",
        forced: "False",
      },
    ],
    mustContain: [],
    mustNotContain: [],
    originalFormat: false,
    tag: null,
  };
}

function serve({
  profiles = [profile(1, "hu", "Hungarian")],
  general = {},
  profilesFail = false,
}: {
  profiles?: ReturnType<typeof profile>[];
  general?: Record<string, unknown>;
  profilesFail?: boolean;
} = {}) {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", enabled_providers: [], ...general },
        discover: {
          tmdb_configured: true,
          metadata_revision: "feed-one",
          locale: "en-US",
        },
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json(languages)),
    http.get("/api/system/languages/profiles", () =>
      profilesFail
        ? new HttpResponse(null, { status: 500 })
        : HttpResponse.json(profiles),
    ),
    http.get(
      "/api/provider-hub/providers",
      () => new HttpResponse(null, { status: 503 }),
    ),
    http.get("/api/discover/metadata/status", () =>
      HttpResponse.json({ data: envelope }),
    ),
    http.get("/api/discover/metadata/search", () =>
      HttpResponse.json({ data: { ...envelope, items: [movie] } }),
    ),
    http.get("/api/discover/metadata/movies/42", () =>
      HttpResponse.json({ data: { ...envelope, item: movie } }),
    ),
  );
}

function open() {
  const router = createMemoryRouter(
    [
      { path: "/discover", element: <Discover /> },
      { path: "/subtitle-hub", element: <div>Subtitle Hub</div> },
    ],
    { initialEntries: ["/discover"] },
  );
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup(), router };
}

async function enterDetail(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Search"), "Northern");
  await user.click(
    await screen.findByRole("button", { name: "Northern Light (2008)" }),
  );
  await screen.findByRole("heading", { name: "Northern Light" });
  await waitFor(() =>
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0080274"),
  );
}

beforeEach(() => {
  localStorage.clear();
  queryClient.clear();
});

it("seeds the language from the reader's own profile and says where it came from", async () => {
  serve();
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("Hungarian"),
  );
  expect(
    screen.getByText(/Preselected from your language profile/),
  ).toBeInTheDocument();
  // In detail the selected title plus the seeded language complete the form,
  // so Find becomes available. Seeding still only fills the language field.
  expect(screen.getByRole("button", { name: /Find subtitles/ })).toBeEnabled();
});

it("prefers the profile the media type actually defaults to", async () => {
  serve({
    profiles: [profile(1, "hu", "Hungarian"), profile(2, "de", "German")],
    general: { movie_default_enabled: true, movie_default_profile: 2 },
  });
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("German"),
  );
});

it("never overrides a language the reader already chose", async () => {
  localStorage.setItem("bazarr.discover.subtitle-language", "eng");
  serve();
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("English"),
  );
  // A remembered choice is the reader's own, so it is not a seed and does not
  // carry the disclosure.
  expect(
    screen.queryByText(/Preselected from your language profile/),
  ).not.toBeInTheDocument();
});

it("does not seed at all when there is no profile to seed from", async () => {
  serve({ profiles: [] });
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await screen.findByRole("button", { name: /Find subtitles/ });
  expect(selectInput("Subtitle language")).toHaveValue("");
  expect(
    screen.queryByText(/Preselected from your language profile/),
  ).not.toBeInTheDocument();
});

it("does not seed when the profile list cannot be read", async () => {
  serve({ profilesFail: true });
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await screen.findByRole("button", { name: /Find subtitles/ });
  expect(selectInput("Subtitle language")).toHaveValue("");
});

it("drops the disclosure once the reader changes the language, and does not seed again", async () => {
  serve();
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("Hungarian"),
  );
  await pickOption(user, "Subtitle language", "English");
  expect(selectInput("Subtitle language")).toHaveValue("English");
  await waitFor(() =>
    expect(
      screen.queryByText(/Preselected from your language profile/),
    ).not.toBeInTheDocument(),
  );
  // The seed happens once per visit, so a re-render cannot put it back.
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(selectInput("Subtitle language")).toHaveValue("English");
});

it("seeds from the profile language, never from the metadata locale or the film region", async () => {
  // A locale and a region that both disagree with the profile. The seed has to
  // follow the profile, because the other two are different questions.
  serve({
    general: { movie_default_enabled: false },
  });
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          theme: "auto",
          enabled_providers: [],
          movie_default_enabled: false,
        },
        discover: {
          tmdb_configured: true,
          metadata_revision: "feed-one",
          locale: "de-DE",
        },
      }),
    ),
  );
  const { user } = open();
  expect(
    screen.queryByRole("combobox", { name: /Subtitle language/ }),
  ).not.toBeInTheDocument();
  await enterDetail(user);
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("Hungarian"),
  );
});
