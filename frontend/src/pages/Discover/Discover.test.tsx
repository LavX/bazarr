/* eslint-disable camelcase -- API fixtures retain transport field names. */
import { createMemoryRouter, Link, RouterProvider } from "react-router";
import { Button, useMantineColorScheme } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import queryClient from "@/apis/queries";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import { setAuthenticated } from "@/utilities/event";
import { readableTime } from "./feedText";
import {
  chooseSegment,
  findSelectInput,
  pickOption,
  selectInput,
} from "./selectTestHelpers";
import Discover from ".";

function ThemeToggle() {
  const { toggleColorScheme } = useMantineColorScheme();
  return <Button onClick={() => toggleColorScheme()}>Change appearance</Button>;
}

function renderDiscover() {
  const router = createMemoryRouter(
    [
      {
        path: "/discover",
        element: (
          <>
            <ThemeToggle />
            <Discover />
          </>
        ),
      },
      {
        path: "/subtitle-hub",
        element: <Link to="/discover">Return to Discover</Link>,
      },
    ],
    { initialEntries: ["/discover"] },
  );
  const view = rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  return { user: userEvent.setup(), router, view };
}

const target = {
  media_type: "movie",
  imdb_id: "tt0133093",
  language: "eng",
  matching_mode: "title",
};
function snapshot(release = "The.Matrix.1999.1080p") {
  return {
    search_id: "search-1",
    context: target,
    status: "complete",
    checked_at: "2026-09-08T10:00:00Z",
    attempted_at: "2026-09-08T10:00:00Z",
    cache_status: "fresh",
    coverage: {
      complete: true,
      configured_count: 1,
      completed_count: 1,
      providers: [
        {
          provider: "example",
          status: "success",
          result_count: 1,
          reason: null,
          retry_at: null,
          elapsed_ms: 2,
        },
      ],
    },
    results: [
      {
        id: "opaque-1",
        search_id: "search-1",
        provider: "example",
        language: "en",
        language_variant: null,
        release,
        scope: "unknown",
        hearing_impaired: null,
        matches: ["imdb_id"],
        compatibility_score: 10,
        compatibility_score_max: 119,
        rating: null,
        uploader: null,
        checked_at: "2026-09-08T10:00:00Z",
        expires_at: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
        stale: false,
      },
    ],
  };
}

beforeEach(() => {
  localStorage.clear();
  server.use(
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { name: "English", code2: "en", code3: "eng", enabled: false },
        { name: "Hungarian", code2: "hu", code3: "hun", enabled: true },
      ]),
    ),
  );
});

async function selectTarget(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
  await pickOption(user, "Subtitle language", "English");
}

describe("Discover retrieval", () => {
  it("requires explicit language and Find subtitles; appearance and query refetch never search", async () => {
    const requests: unknown[] = [];
    server.use(
      http.post("/api/discover/search", async ({ request }) => {
        requests.push(await request.json());
        return HttpResponse.json(snapshot());
      }),
    );
    const { user } = renderDiscover();
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    expect(selectInput("Subtitle language")).toHaveValue("");
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    await queryClient.refetchQueries({ type: "active" });
    expect(requests).toEqual([]);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    expect(
      await screen.findByText("The.Matrix.1999.1080p"),
    ).toBeInTheDocument();
    expect(requests).toEqual([
      {
        media_type: "movie",
        imdb_id: "tt0133093",
        language: "eng",
        refresh: false,
      },
    ]);
    await queryClient.refetchQueries({ type: "active" });
    expect(requests).toHaveLength(1);
    expect(screen.getByText("HI unknown")).toBeInTheDocument();
  });

  it("keeps the saved task through settings navigation and appearance changes", async () => {
    server.use(
      http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("The.Matrix.1999.1080p");
    await user.click(screen.getByRole("link", { name: "Provider settings" }));
    await user.click(screen.getByRole("link", { name: "Return to Discover" }));
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Change appearance" }));
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  });

  it("retains prior results and checked time after a failed refresh", async () => {
    let attempt = 0;
    server.use(
      http.post("/api/discover/search", () => {
        attempt += 1;
        return attempt === 1
          ? HttpResponse.json(snapshot())
          : HttpResponse.json({ message: "Unavailable" }, { status: 503 });
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("The.Matrix.1999.1080p");
    await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
    expect(await screen.findByText(/Refresh failed/)).toBeInTheDocument();
    expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
    expect(
      screen.getAllByText(readableTime("2026-09-08T10:00:00Z"))[0],
    ).toHaveAttribute("datetime", "2026-09-08T10:00:00Z");
  });

  it("accepts a partial server snapshot without resurrecting an omitted handle", async () => {
    const first = snapshot("Evicted result");
    const retained = {
      ...first.results[0],
      id: "still-valid",
      release: "Usable result",
    };
    first.results.push(retained);
    let attempt = 0;
    server.use(
      http.post("/api/discover/search", () => {
        attempt += 1;
        return HttpResponse.json(
          attempt === 1
            ? first
            : {
                ...first,
                status: "partial",
                cache_status: "stale",
                results: [{ ...retained, stale: true }],
                coverage: {
                  ...first.coverage,
                  complete: false,
                  completed_count: 0,
                  providers: [
                    {
                      ...first.coverage.providers[0],
                      status: "authentication_required",
                      result_count: 0,
                    },
                  ],
                },
              },
        );
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await screen.findByText("Evicted result");
    await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
    await screen.findByText("Sign in to this provider in provider settings");
    expect(screen.queryByText("Evicted result")).not.toBeInTheDocument();
    expect(screen.getByText("Usable result")).toBeInTheDocument();
    expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
    expect(
      screen.getAllByText(readableTime(first.checked_at))[0],
    ).toHaveAttribute("datetime", first.checked_at);
  });

  it.each([false, true])(
    "drops expired handles after transport failure, with usable remainder %s",
    async (hasUsable) => {
      const first = snapshot("Expired result");
      const expiry = Date.now() + 60 * 60 * 1000;
      first.results[0].expires_at = new Date(expiry).toISOString();
      if (hasUsable)
        first.results.push({
          ...first.results[0],
          id: "still-valid",
          release: "Usable result",
          expires_at: new Date(expiry + 60 * 60 * 1000).toISOString(),
        });
      let attempt = 0;
      server.use(
        http.post("/api/discover/search", () => {
          attempt += 1;
          return attempt === 1
            ? HttpResponse.json(first)
            : HttpResponse.json({}, { status: 503 });
        }),
      );
      const { user } = renderDiscover();
      await selectTarget(user);
      await user.click(screen.getByRole("button", { name: "Find subtitles" }));
      await screen.findByText("Expired result");
      const now = vi.spyOn(Date, "now").mockReturnValue(expiry + 1);
      try {
        await user.click(
          screen.getByRole("button", { name: "Refresh subtitles" }),
        );
        await screen.findByText(hasUsable ? /Refresh failed/ : /Search failed/);
        expect(screen.queryByText("Expired result")).not.toBeInTheDocument();
        expect(
          screen.queryByText(/No subtitles matched this title and language/),
        ).not.toBeInTheDocument();
        if (hasUsable)
          expect(screen.getByText("Usable result")).toBeInTheDocument();
        expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
        expect(
          screen.getAllByText(readableTime(first.checked_at))[0],
        ).toHaveAttribute("datetime", first.checked_at);
        if (!hasUsable) {
          let complete: (() => void) | undefined;
          server.use(
            http.post("/api/discover/search", async () => {
              await new Promise<void>((resolve) => {
                complete = resolve;
              });
              return HttpResponse.json({
                ...first,
                results: [],
                coverage: {
                  ...first.coverage,
                  providers: [
                    {
                      ...first.coverage.providers[0],
                      status: "empty",
                      result_count: 0,
                    },
                  ],
                },
              });
            }),
          );
          await user.click(
            screen.getByRole("button", { name: "Refresh subtitles" }),
          );
          await waitFor(() => expect(complete).toBeDefined());
          expect(screen.getByText(/Searching providers/)).toBeInTheDocument();
          expect(
            screen.queryByText(/No subtitles matched this title and language/),
          ).not.toBeInTheDocument();
          complete?.();
          expect(
            await screen.findByText(
              /No subtitles matched this title and language/,
            ),
          ).toBeInTheDocument();
        }
      } finally {
        now.mockRestore();
      }
    },
  );

  it("ignores a late response after the language changes, and logout clears the task", async () => {
    let release: (() => void) | undefined;
    server.use(
      http.post("/api/discover/search", async () => {
        await new Promise<void>((resolve) => {
          release = resolve;
        });
        return HttpResponse.json(snapshot());
      }),
    );
    const { user } = renderDiscover();
    await selectTarget(user);
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    await waitFor(() => expect(release).toBeDefined());
    await pickOption(user, "Subtitle language", "Hungarian");
    release?.();
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "Find subtitles" }),
      ).toBeEnabled(),
    );
    expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
    setAuthenticated(false);
    await waitFor(() =>
      expect(screen.getByLabelText("IMDb ID")).toHaveValue(""),
    );
  });

  it("blocks malformed IMDb identity and incomplete exact episodes", async () => {
    const { user } = renderDiscover();
    await selectTarget(user);
    await chooseSegment(user, "Episode");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    await user.type(screen.getByLabelText("Season"), "0");
    await user.type(screen.getByRole("textbox", { name: "Episode" }), "3");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
    await user.click(
      screen.getByLabelText(
        "I confirm this series IMDb ID and the manual season and episode numbers",
      ),
    );
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeEnabled();
    await user.clear(screen.getByLabelText("IMDb ID"));
    await user.type(screen.getByLabelText("IMDb ID"), "The Matrix");
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled();
  });
});

it("distinguishes successful empty, partial, failed and provider setup states", async () => {
  const full = snapshot();
  const responses = [
    {
      ...full,
      results: [],
      coverage: {
        ...full.coverage,
        providers: [
          { ...full.coverage.providers[0], status: "empty", result_count: 0 },
        ],
      },
    },
    {
      ...full,
      status: "partial",
      coverage: {
        ...full.coverage,
        complete: false,
        configured_count: 2,
        providers: [
          ...full.coverage.providers,
          {
            provider: "needs-login",
            status: "authentication_required",
            result_count: 0,
            reason: "authentication_required",
            elapsed_ms: 1,
            retry_at: null,
          },
        ],
      },
    },
    {
      ...full,
      status: "failed",
      results: [],
      coverage: {
        complete: false,
        configured_count: 1,
        completed_count: 0,
        providers: [
          { ...full.coverage.providers[0], status: "timeout", result_count: 0 },
        ],
      },
    },
    {
      ...full,
      status: "failed",
      results: [],
      coverage: {
        complete: false,
        configured_count: 0,
        completed_count: 0,
        providers: [],
      },
    },
  ];
  server.use(
    http.post("/api/discover/search", () =>
      HttpResponse.json(responses.shift()),
    ),
  );
  const { user } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  expect(await screen.findByText(/No subtitles matched/)).toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
  expect(
    await screen.findByText(/Some providers could not complete/),
  ).toBeInTheDocument();
  expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  expect(
    screen.getByText("Sign in to this provider in provider settings"),
  ).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
  await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
  expect(await screen.findByText(/No provider completed/)).toBeInTheDocument();
  expect(screen.getByText("Provider search timed out")).toBeInTheDocument();
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
  await user.click(screen.getByRole("button", { name: "Refresh subtitles" }));
  expect(
    await screen.findByText("Set up a subtitle provider"),
  ).toBeInTheDocument();
});

it("remembers only the explicit language and works when browser storage is blocked", async () => {
  const original = Storage.prototype.setItem;
  const blocked = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(function (this: Storage, key, value) {
      if (key === "bazarr.discover.subtitle-language")
        throw new DOMException("Storage blocked", "SecurityError");
      original.call(this, key, value);
    });
  try {
    const { user } = renderDiscover();
    await selectTarget(user);
    expect(screen.getByText(/remembered for this visit/)).toBeInTheDocument();
    expect(selectInput("Subtitle language")).toHaveValue("English");
    await user.click(screen.getByRole("link", { name: "Provider settings" }));
    await user.click(screen.getByRole("link", { name: "Return to Discover" }));
    expect(selectInput("Subtitle language")).toHaveValue("English");
  } finally {
    blocked.mockRestore();
  }
});

// The seeded language is written when the first search uses it, and that write
// is the same evidence about storage as any other. Its answer was being thrown
// away, so a reader whose storage refuses writes was told nothing and believed
// the seeded language would be there next time.
it("says a seeded language is session-only once the search tries to remember it", async () => {
  server.use(
    http.get("/api/system/languages/profiles", () =>
      HttpResponse.json([
        {
          profileId: 1,
          name: "English",
          cutoff: null,
          items: [
            {
              id: 1,
              language: "en",
              audio_exclude: "False",
              hi: "False",
              forced: "False",
            },
          ],
          mustContain: [],
          mustNotContain: [],
          originalFormat: false,
          tag: null,
        },
      ]),
    ),
    http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
  );
  const original = Storage.prototype.setItem;
  const blocked = vi
    .spyOn(Storage.prototype, "setItem")
    .mockImplementation(function (this: Storage, key, value) {
      if (key === "bazarr.discover.subtitle-language")
        throw new DOMException("Storage blocked", "SecurityError");
      original.call(this, key, value);
    });
  try {
    const { user } = renderDiscover();
    await waitFor(() =>
      expect(selectInput("Subtitle language")).toHaveValue("English"),
    );
    await user.type(screen.getByLabelText("IMDb ID"), "tt0133093");
    // Seeding alone writes nothing, so there is nothing to report yet.
    expect(screen.queryByText(/remembered for this visit/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "Find subtitles" }));
    expect(
      await screen.findByText(/remembered for this visit/),
    ).toBeInTheDocument();
  } finally {
    blocked.mockRestore();
  }
});

it("restores an explicit preference without deriving one from enabled library languages", async () => {
  localStorage.setItem("bazarr.discover.subtitle-language", "eng");
  renderDiscover();
  // The stored code is shown by its name once the language list has loaded.
  await findSelectInput("Subtitle language");
  await waitFor(() =>
    expect(selectInput("Subtitle language")).toHaveValue("English"),
  );
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("");
  expect(screen.getByRole("button", { name: "Find subtitles" })).toBeDisabled();
});

it("preserves results when the shell replaces its router", async () => {
  server.use(
    http.post("/api/discover/search", () => HttpResponse.json(snapshot())),
  );
  const { user, view } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await screen.findByText("The.Matrix.1999.1080p");
  const replacement = createMemoryRouter(
    [{ path: "/discover", element: <Discover /> }],
    { initialEntries: ["/discover"] },
  );
  view.rerender(
    <AllProviders>
      <RouterProvider router={replacement} />
    </AllProviders>,
  );
  expect(screen.getByText("The.Matrix.1999.1080p")).toBeInTheDocument();
  expect(screen.getByLabelText("IMDb ID")).toHaveValue("tt0133093");
});

it("does not restore a logged-out task when its last request finishes", async () => {
  let finish: (() => void) | undefined;
  server.use(
    http.post("/api/discover/search", async () => {
      await new Promise<void>((resolve) => {
        finish = resolve;
      });
      return HttpResponse.json(snapshot());
    }),
  );
  const { user } = renderDiscover();
  await selectTarget(user);
  await user.click(screen.getByRole("button", { name: "Find subtitles" }));
  await waitFor(() => expect(finish).toBeDefined());
  setAuthenticated(false);
  await waitFor(() => expect(screen.getByLabelText("IMDb ID")).toHaveValue(""));
  finish?.();
  await waitFor(() =>
    expect(
      screen.getByRole("button", { name: "Find subtitles" }),
    ).toBeDisabled(),
  );
  expect(screen.queryByText("The.Matrix.1999.1080p")).not.toBeInTheDocument();
});
