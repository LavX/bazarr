/* eslint-disable camelcase -- transport field names. */
import type { ReactElement } from "react";
import { MantineProvider } from "@mantine/core";
import { render as rtlRender, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { MetadataTitle } from "@/types/discover";
import type { SeerrMediaState } from "@/types/seerr";
import SeerrRequestModal from "./SeerrRequestModal";
import styles from "./Discover.module.scss";

function render(ui: ReactElement) {
  return rtlRender(<MantineProvider>{ui}</MantineProvider>);
}

const movie = {
  source: "tmdb",
  media_type: "movie",
  id: 550,
  title: "Fight Club",
  year: 1999,
  imdb_id: "tt0137523",
  source_id: "tmdb:movie:550",
  mapping_status: "resolved",
} as MetadataTitle;

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
} as MetadataTitle;

const base: SeerrMediaState = {
  configured: true,
  known: true,
  status: "unknown",
  status_4k: "unknown",
  request: null,
  seasons: [],
  requestable: true,
  requestable_4k: false,
  partial_requests: true,
  special_episodes: false,
  link: null,
};

describe("SeerrRequestModal", () => {
  it("renders each season group's legend as a span, and applies the group class", () => {
    render(
      <SeerrRequestModal
        title={show}
        state={base}
        tmdbId={1399}
        onClose={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );
    const ownedLegend = screen.getByText("In your Bazarr+ library");
    const openLegend = screen.getByText("Available to request");
    // A <Text> renders as <p> by default: nesting one inside a <legend> is
    // invalid HTML, so both labels must render as inline spans instead.
    expect(ownedLegend.tagName).toBe("SPAN");
    expect(openLegend.tagName).toBe("SPAN");
    // A <fieldset> with a <legend> carries the implicit "group" role, named
    // by its legend, so both groups are reachable without raw node access.
    const groups = [
      screen.getByRole("group", { name: "In your Bazarr+ library" }),
      screen.getByRole("group", { name: "Available to request" }),
    ];
    groups.forEach((group) => {
      expect(group.className).toContain(styles.seerrGroup);
    });
  });

  it("hides the 4K checkbox and labels the submit clearly when only the 4K lane is open for a movie", async () => {
    const onSubmit = vi.fn();
    render(
      <SeerrRequestModal
        title={movie}
        state={{ ...base, requestable: false, requestable_4k: true }}
        tmdbId={550}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(
      screen.getByText("Only the 4K version is available to request."),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Request the 4K version" }),
    );
    expect(onSubmit).toHaveBeenCalledWith({
      media_type: "movie",
      tmdb_id: 550,
      is4k: true,
    });
  });

  it("collapses a show to request-all-seasons with is4k when only the 4K lane is open", async () => {
    const onSubmit = vi.fn();
    render(
      <SeerrRequestModal
        title={show}
        state={{ ...base, requestable: false, requestable_4k: true }}
        tmdbId={1399}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    // No invented per-season 4K state: the seasons UI does not render at all.
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.queryByText("Available to request")).toBeNull();
    expect(screen.queryByText("In your Bazarr+ library")).toBeNull();
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(onSubmit).toHaveBeenCalledWith({
      media_type: "tv",
      tmdb_id: 1399,
      tvdb_id: 121361,
      seasons: "all",
      is4k: true,
    });
  });

  it("collapses a show whose every season is taken to the 4K whole-series request", async () => {
    const onSubmit = vi.fn();
    render(
      <SeerrRequestModal
        title={show}
        // A show stays `requestable` once every season is taken: the flag only
        // reports that Seerr has not blocklisted it. The season arithmetic is
        // what says the non-4K lane has nothing left.
        state={{
          ...base,
          status: "available",
          requestable: true,
          requestable_4k: true,
          seasons: [
            { number: 1, state: "available" },
            { number: 2, state: "available" },
          ],
        }}
        tmdbId={1399}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    expect(
      screen.getByText("Only the 4K version is available to request."),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(onSubmit).toHaveBeenCalledWith({
      media_type: "tv",
      tmdb_id: 1399,
      tvdb_id: 121361,
      seasons: "all",
      is4k: true,
    });
  });

  it("collapses the season picker once 4K is ticked for a show", async () => {
    const onSubmit = vi.fn();
    render(
      <SeerrRequestModal
        title={show}
        state={{ ...base, requestable: true, requestable_4k: true }}
        tmdbId={1399}
        onClose={vi.fn()}
        onSubmit={onSubmit}
      />,
    );
    await userEvent.click(screen.getByRole("checkbox", { name: "Season 1" }));
    await userEvent.click(
      screen.getByRole("checkbox", { name: "Request the 4K version" }),
    );
    // The groups describe the non-4K lane only, so a 4K request cannot carry
    // them: the picker, and the season ticked in it, go away.
    expect(screen.queryByText("Available to request")).toBeNull();
    expect(
      screen.getByText(
        "Seerr does not report 4K availability season by season, so the whole series is requested.",
      ),
    ).toBeInTheDocument();
    await userEvent.click(
      screen.getByRole("button", { name: "Request all seasons" }),
    );
    expect(onSubmit).toHaveBeenCalledWith({
      media_type: "tv",
      tmdb_id: 1399,
      tvdb_id: 121361,
      seasons: "all",
      is4k: true,
    });
  });

  it("says so when the library check behind the owned group is incomplete", () => {
    render(
      <SeerrRequestModal
        title={show}
        state={base}
        tmdbId={1399}
        ownershipUncertain
        onClose={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );
    expect(
      screen.getByText(/Your library check is incomplete/),
    ).toBeInTheDocument();
  });

  it("keeps the optional 4K checkbox unchecked when both lanes are open for a movie", () => {
    render(
      <SeerrRequestModal
        title={movie}
        state={{ ...base, requestable: true, requestable_4k: true }}
        tmdbId={550}
        onClose={vi.fn()}
        onSubmit={vi.fn()}
      />,
    );
    expect(
      screen.getByRole("checkbox", { name: "Request the 4K version" }),
    ).not.toBeChecked();
    expect(
      screen.getByRole("button", { name: "Request in Seerr" }),
    ).toBeInTheDocument();
  });
});
