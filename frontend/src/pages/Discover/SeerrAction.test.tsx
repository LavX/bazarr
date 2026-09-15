/* eslint-disable camelcase -- API fixture and transport field names. */
import type { ReactElement } from "react";
import { MemoryRouter } from "react-router";
import { MantineProvider } from "@mantine/core";
import { render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SeerrMediaResponse } from "@/types/seerr";
import SeerrAction from "./SeerrAction";

// A plain testing-library render, not the page-harness `@/tests` render: this
// suite mocks the Seerr and system-settings hooks directly and never touches
// React Query or the Discover context, so it only needs enough context for
// SeerrAction's own Mantine components and its one react-router Link to
// mount.
function render(ui: ReactElement) {
  return rtlRender(
    <MemoryRouter>
      <MantineProvider>{ui}</MantineProvider>
    </MemoryRouter>,
  );
}

const media = vi.fn();
const request = vi.fn();
// Configurable per test (default true) so the "Seerr disabled" rule has
// somewhere to flip it off, rather than being permanently hardcoded on.
let useSeerrEnabled = true;
vi.mock("@/apis/hooks/system", () => ({
  useSystemSettings: () => ({
    data: { general: { use_seerr: useSeerrEnabled } },
  }),
}));
vi.mock("@/apis/hooks/seerr", () => ({
  useSeerrMedia: (identity: unknown, enabled: boolean) =>
    media(identity, enabled),
  useSeerrRequestMutation: () => ({ mutate: request, isPending: false }),
}));

// Deliberately loose fixtures: SeerrAction only reads a handful of fields off
// `title`, so the JSX usage sites cast to `never` rather than the whole
// MetadataTitle union. Leaving these untyped (instead of `as never` here)
// keeps them real object types, so spreading one to build the OMDb-only
// fixture below still type-checks.
const movie = {
  source: "tmdb",
  media_type: "movie",
  id: 550,
  title: "Fight Club",
  year: 1999,
  imdb_id: "tt0137523",
  source_id: "tmdb:movie:550",
  mapping_status: "resolved",
};
const show = {
  source: "tmdb",
  media_type: "show",
  id: 1399,
  tvdb_id: 121361,
  title: "Game of Thrones",
  seasons: [
    { id: 1, season: 1, title: "Season 1", episode_count: 10 },
    { id: 2, season: 2, title: "Season 2", episode_count: 10 },
  ],
  ownership: {
    seasons_owned: [1],
    episode_count: 10,
    unknown_owners: false,
    truncated: false,
    selected_episode_owned: null,
    complete_series: null,
  },
};

function answer(data: SeerrMediaResponse | undefined, extra = {}) {
  media.mockReturnValue({
    data,
    isPending: data === undefined,
    isError: false,
    refetch: vi.fn(),
    ...extra,
  });
}

const base = {
  configured: true as const,
  known: true,
  status_4k: "unknown" as const,
  request: null,
  seasons: [],
  requestable: false,
  requestable_4k: false,
  partial_requests: true,
  special_episodes: false,
  link: "http://s/movie/550",
};

describe("SeerrAction", () => {
  // The rejected-key once-per-session flag lives in real sessionStorage, so
  // tests that touch it (or that must prove it starts unset) need a clean
  // slate rather than whatever an earlier test in this file left behind.
  beforeEach(() => {
    useSeerrEnabled = true;
    media.mockClear();
    // Reset, not clear: one test below installs an implementation that answers
    // the mutate handlers, and it must not leak into the next.
    request.mockReset();
    try {
      sessionStorage.clear();
    } catch {
      /* storage unavailable */
    }
  });

  it("renders nothing when Seerr is disabled, even with a requestable title", () => {
    useSeerrEnabled = false;
    answer({ ...base, known: false, status: "unknown", requestable: true });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.queryByText(/Seerr/)).toBeNull();
    expect(media).toHaveBeenLastCalledWith(null, false);
  });

  it("renders nothing for an OMDb-only title", () => {
    answer(undefined);
    render(
      <SeerrAction
        title={{ ...movie, source: "omdb", id: "tt0137523" } as never}
        inLibrary={false}
      />,
    );
    expect(screen.queryByText(/Seerr/)).toBeNull();
    expect(media).toHaveBeenLastCalledWith(null, false);
  });

  it("requests a movie in one click and discloses the owner", async () => {
    answer({ ...base, known: false, status: "unknown", requestable: true });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Request in Seerr" }),
    );
    expect(request).toHaveBeenCalledWith(
      { media_type: "movie", tmdb_id: 550, is4k: false },
      expect.anything(),
    );
    expect(
      screen.getByText(/Requested as the Seerr owner and approved immediately/),
    ).toBeInTheDocument();
  });

  it("hides the request for a movie already in the library that Seerr does not know", () => {
    answer({ ...base, known: false, status: "unknown", requestable: true });
    render(<SeerrAction title={movie as never} inLibrary={true} />);
    expect(
      screen.queryByRole("button", { name: "Request in Seerr" }),
    ).toBeNull();
  });

  it.each([
    [
      {
        status: "pending",
        request: { id: 1, status: "pending", is4k: false, seasons: [] },
      },
      "Awaiting approval",
    ],
    [
      {
        status: "processing",
        request: { id: 1, status: "approved", is4k: false, seasons: [] },
      },
      "Processing",
    ],
    [
      {
        status: "unknown",
        requestable: true,
        request: { id: 1, status: "declined", is4k: false, seasons: [] },
      },
      "Declined",
    ],
    [
      {
        status: "unknown",
        requestable: true,
        request: { id: 1, status: "failed", is4k: false, seasons: [] },
      },
      "Failed",
    ],
    [{ status: "available" }, "Available in Seerr"],
    [{ status: "blocklisted" }, "Blocklisted"],
  ] as const)("renders %o as %s", (partial, label) => {
    answer({ ...base, ...partial } as SeerrMediaResponse);
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.getByText(label)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open in Seerr/ })).toHaveAttribute(
      "href",
      "http://s/movie/550",
    );
  });

  it("re-enables the action after a declined request", () => {
    answer({
      ...base,
      status: "unknown",
      requestable: true,
      request: { id: 1, status: "declined", is4k: false, seasons: [] },
    });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(
      screen.getByRole("button", { name: "Request in Seerr" }),
    ).toBeEnabled();
  });

  it("distinguishes unreachable from a rejected key", () => {
    answer({ configured: true, error_code: "unreachable" });
    const { unmount } = render(
      <SeerrAction title={movie as never} inLibrary={false} />,
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
    unmount();
    answer({ configured: true, error_code: "rejected_key" });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.getByRole("link", { name: /settings/i })).toHaveAttribute(
      "href",
      "/settings/connections#seerr",
    );
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("opens the seasons flow for a show with three labelled groups and nothing preselected", async () => {
    answer({
      ...base,
      status: "partially_available",
      requestable: true,
      link: "http://s/tv/1399",
      seasons: [{ number: 2, state: "requested" }],
    });
    render(<SeerrAction title={show as never} inLibrary={true} />);
    expect(screen.getByText("Some seasons available")).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Request seasons" }),
    );
    expect(screen.getByText("Already in Seerr")).toBeInTheDocument();
    expect(screen.getByText("In your Bazarr+ library")).toBeInTheDocument();
    expect(screen.getByText("Available to request")).toBeInTheDocument();
    expect(screen.queryAllByRole("checkbox", { checked: true })).toHaveLength(
      0,
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Season 1" }));
    await userEvent.click(
      screen.getByRole("button", { name: "Request 1 season" }),
    );
    expect(request).toHaveBeenCalledWith(
      {
        media_type: "tv",
        tmdb_id: 1399,
        tvdb_id: 121361,
        seasons: [1],
        is4k: false,
      },
      expect.anything(),
    );
  });

  it("collapses to request-all when Seerr forbids partial requests", async () => {
    answer({
      ...base,
      known: false,
      status: "unknown",
      requestable: true,
      partial_requests: false,
      link: "http://s/tv/1399",
    });
    render(<SeerrAction title={show as never} inLibrary={false} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Request in Seerr" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(request).toHaveBeenCalledWith(
      {
        media_type: "tv",
        tmdb_id: 1399,
        tvdb_id: 121361,
        seasons: "all",
        is4k: false,
      },
      expect.anything(),
    );
  });

  it("makes the 4K lane reachable for a movie whose only open lane is 4K", async () => {
    answer({
      ...base,
      status: "blocklisted",
      requestable: false,
      requestable_4k: true,
    });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    const button = screen.getByRole("button", { name: "Request in Seerr" });
    await userEvent.click(button);
    // The first click must open the modal, never submit directly.
    expect(request).not.toHaveBeenCalled();
    await userEvent.click(
      screen.getByRole("button", { name: "Request the 4K version" }),
    );
    expect(request).toHaveBeenCalledWith(
      { media_type: "movie", tmdb_id: 550, is4k: true },
      expect.anything(),
    );
  });

  it("offers request-all-seasons with is4k for a show whose only open lane is 4K", async () => {
    answer({
      ...base,
      status: "blocklisted",
      requestable: false,
      requestable_4k: true,
      link: "http://s/tv/1399",
    });
    render(<SeerrAction title={show as never} inLibrary={false} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Request in Seerr" }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(request).toHaveBeenCalledWith(
      {
        media_type: "tv",
        tmdb_id: 1399,
        tvdb_id: 121361,
        seasons: "all",
        is4k: true,
      },
      expect.anything(),
    );
  });

  it("hides the request action for a fully requested show with no 4K lane open", () => {
    answer({
      ...base,
      status: "available",
      requestable: true,
      requestable_4k: false,
      seasons: [
        { number: 1, state: "available" },
        { number: 2, state: "available" },
      ],
    });
    render(<SeerrAction title={show as never} inLibrary={true} />);
    expect(screen.queryByRole("button", { name: /Request/ })).toBeNull();
  });

  it("keeps the request action for an otherwise-exhausted show, and its 4K request submits", async () => {
    answer({
      ...base,
      status: "available",
      requestable: true,
      requestable_4k: true,
      seasons: [
        { number: 1, state: "available" },
        { number: 2, state: "available" },
      ],
    });
    render(<SeerrAction title={show as never} inLibrary={true} />);
    await userEvent.click(
      screen.getByRole("button", { name: "Request in Seerr" }),
    );
    // The modal has to reach a submittable state: a show keeps `requestable`
    // once Seerr holds every season, so a 4K-only lane cannot be recognised
    // from that flag and the submit used to stay disabled with nothing to tick.
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(request).toHaveBeenCalledWith(
      {
        media_type: "tv",
        tmdb_id: 1399,
        tvdb_id: 121361,
        seasons: "all",
        is4k: true,
      },
      expect.anything(),
    );
  });

  it("reads availability off the media row, not off a request Seerr left approved", () => {
    // Seerr does not tidy a finished request away, so an available title
    // routinely still carries an APPROVED row. The badge must report the
    // title, not the paperwork.
    const approved = { id: 1, status: "approved", is4k: false, seasons: [] };
    answer({ ...base, status: "available", request: approved } as never);
    const { unmount } = render(
      <SeerrAction title={movie as never} inLibrary={false} />,
    );
    expect(screen.getByText("Available in Seerr")).toBeInTheDocument();
    expect(screen.queryByText("Processing")).toBeNull();
    unmount();
    answer({
      ...base,
      status: "partially_available",
      requestable: true,
      request: approved,
      seasons: [{ number: 2, state: "available" }],
    } as never);
    render(<SeerrAction title={show as never} inLibrary={true} />);
    expect(screen.getByText("Some seasons available")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Request seasons" }),
    ).toBeInTheDocument();
  });

  it("does not carry a request notice over to the next title", async () => {
    request.mockImplementation((_body, handlers) =>
      handlers.onSuccess({ outcome: "requested" }),
    );
    answer({ ...base, known: false, status: "unknown", requestable: true });
    const { rerender } = render(
      <SeerrAction title={movie as never} inLibrary={false} />,
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Request in Seerr" }),
    );
    expect(screen.getByText("Requested in Seerr.")).toBeInTheDocument();
    // Same mounted component, a different selected title: the notice belongs
    // to the title it was raised for.
    rerender(
      <MemoryRouter>
        <MantineProvider>
          <SeerrAction
            title={{ ...movie, id: 680, title: "Pulp Fiction" } as never}
            inLibrary={false}
          />
        </MantineProvider>
      </MemoryRouter>,
    );
    expect(screen.queryByText("Requested in Seerr.")).toBeNull();
  });

  it("says the library check is incomplete next to the action", () => {
    answer({ ...base, known: false, status: "unknown", requestable: true });
    render(
      <SeerrAction title={movie as never} inLibrary={false} libraryUncertain />,
    );
    expect(
      screen.getByText(/Your library check is incomplete/),
    ).toBeInTheDocument();
  });

  it("resolves a local-source movie by its tmdb_id", () => {
    answer({ ...base, known: false, status: "unknown", requestable: true });
    const localMovie = {
      source: "local",
      media_type: "movie",
      id: 42,
      tmdb_id: 550,
      title: "Fight Club",
    };
    render(<SeerrAction title={localMovie as never} inLibrary={false} />);
    expect(media).toHaveBeenLastCalledWith(
      { kind: "movie", tmdbId: 550 },
      true,
    );
    expect(
      screen.getByRole("button", { name: "Request in Seerr" }),
    ).toBeInTheDocument();
  });

  it("announces a loading state while checking Seerr", () => {
    answer(undefined);
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.getByRole("status")).toHaveTextContent(/Checking Seerr/);
  });

  it("renders nothing when Seerr is not configured", () => {
    answer({ configured: true, error_code: "not_configured" });
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.queryByText(/Seerr/)).toBeNull();
  });

  it("shows the rejected-key banner once per session", () => {
    answer({ configured: true, error_code: "rejected_key" });
    const { unmount } = render(
      <SeerrAction title={movie as never} inLibrary={false} />,
    );
    expect(screen.getByText(/Seerr rejected the API key/)).toBeInTheDocument();
    unmount();
    render(<SeerrAction title={movie as never} inLibrary={false} />);
    expect(screen.queryByText(/Seerr rejected the API key/)).toBeNull();
  });
});
