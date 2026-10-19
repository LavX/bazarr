/* eslint-disable camelcase */

/**
 * The hidden-tracks disclosure of a row another tab or user changed. The
 * socket event a hide's re-index sends refreshes the row, so the track
 * leaves the table; the disclosure that explains and undoes that must
 * refresh with it rather than wait for its cache to go stale.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type {
  EmbeddedHiddenTrack,
  HiddenTrackMediaType,
} from "@/apis/raw/subtitles";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import HiddenTracks from "./HiddenTracks";

// Runs the app's own socket handler for one event, as the socket module does.
function receive(key: string, ids: number[]) {
  const reducer = createDefaultReducer().find((item) => item.key === key);
  if (reducer === undefined) {
    throw new Error(`no socket handler for "${key}"`);
  }
  reducer.any?.();
  (reducer.update as ((payload: number[]) => void) | undefined)?.(ids);
}

function frenchTrack(mediaType: HiddenTrackMediaType): EmbeddedHiddenTrack {
  return {
    media_type: mediaType,
    media_id: 7,
    arr_instance_id: 1,
    language: "fr",
    code2: "fr",
    name: "French",
    hi: false,
    forced: false,
    codec: null,
    hidden_at: null,
  };
}

describe("HiddenTracks", () => {
  // The events a hide's re-index sends for each media type: the movie
  // indexer sends "movie", the episode hide "series", the sports hide
  // "sports". They are the ones that refresh the row itself.
  it.each([
    ["movie", "movie"],
    ["episode", "series"],
    ["sports", "sports"],
  ] as const)(
    "%s: shows a track hidden elsewhere once a %s event arrives",
    async (mediaType, event) => {
      const hidden: EmbeddedHiddenTrack[] = [];
      let requests = 0;
      server.use(
        http.get("/api/subtitles/embedded-hidden", () => {
          requests += 1;
          return HttpResponse.json({ data: [...hidden] });
        }),
      );

      customRender(<HiddenTracks mediaType={mediaType} mediaId={7} />);
      await waitFor(() => expect(requests).toBeGreaterThan(0));
      await waitFor(() => expect(queryClient.isFetching()).toBe(0));
      expect(
        screen.queryByRole("button", { name: /Hidden tracks/ }),
      ).not.toBeInTheDocument();

      // Another tab hides the track; this one only hears the socket event.
      hidden.push(frenchTrack(mediaType));
      receive(event, [42]);

      expect(
        await screen.findByRole("button", { name: "Hidden tracks (1)" }),
      ).toBeInTheDocument();
    },
  );
});

describe("HiddenTracks disclosure", () => {
  function serve(tracks: EmbeddedHiddenTrack[]) {
    server.use(
      http.get("/api/subtitles/embedded-hidden", () =>
        HttpResponse.json({ data: tracks }),
      ),
    );
  }

  async function open(ui: React.ReactElement) {
    const user = userEvent.setup();
    customRender(ui);
    const trigger = await screen.findByRole("button", {
      name: "Hidden tracks (1)",
    });
    await user.click(trigger);
    // jsdom lays nothing out, so the positioning code treats the trigger as
    // off screen and hides the list from the accessibility tree.
    const dialog = await screen.findByRole("dialog", { hidden: true });
    return { trigger, dialog };
  }

  it("names each hidden codec the way the row badges do", async () => {
    serve([{ ...frenchTrack("movie"), codec: "pgs, subrip" }]);
    const { dialog } = await open(
      <HiddenTracks mediaType="movie" mediaId={7} />,
    );

    const pgs = within(dialog).getByTitle("Subtitle format (PGS, image-based)");
    expect(pgs).toHaveTextContent(/^PGS$/);
    expect(
      within(dialog).getByTitle("Subtitle format (SRT)"),
    ).toHaveTextContent(/^SRT$/);
    // Never the stored lowercase value.
    expect(within(dialog).queryByText(/pgs|subrip/)).not.toBeInTheDocument();
  });

  it("shows no codec when none was recorded", async () => {
    serve([frenchTrack("movie")]);
    const { dialog } = await open(
      <HiddenTracks mediaType="movie" mediaId={7} />,
    );

    expect(
      within(dialog).queryByTitle(/^Subtitle format/),
    ).not.toBeInTheDocument();
  });

  it("offers Unhide as a quiet grey action, like its trigger", async () => {
    serve([frenchTrack("movie")]);
    const { trigger, dialog } = await open(
      <HiddenTracks mediaType="movie" mediaId={7} />,
    );

    const unhide = within(dialog).getByRole("button", {
      name: "Unhide French",
      hidden: true,
    });
    expect(unhide).toHaveAttribute("data-variant", "subtle");
    expect(trigger).toHaveAttribute("data-variant", "subtle");
    // Mantine writes the colour into the button's style variables.
    expect(unhide.getAttribute("style")).toBe(trigger.getAttribute("style"));
  });

  it("opens from the start of a trigger inside a row", async () => {
    serve([frenchTrack("episode")]);
    const { trigger, dialog } = await open(
      <HiddenTracks mediaType="episode" mediaId={7} />,
    );

    expect(dialog).toHaveAttribute("data-position", "bottom-start");
    expect(trigger.parentElement?.getAttribute("style") ?? "").not.toContain(
      "flex-end",
    );
  });

  it("sits at the end of its own line under a table and opens from there", async () => {
    serve([frenchTrack("movie")]);
    const { trigger, dialog } = await open(
      <HiddenTracks mediaType="movie" mediaId={7} align="end" />,
    );

    expect(dialog).toHaveAttribute("data-position", "bottom-end");
    // The trigger keeps its own width in a wrapper that pushes it to the end,
    // so the list opens under the words the user clicked, not under the far
    // edge of a button stretched across the page.
    expect(trigger.parentElement?.getAttribute("style")).toContain(
      "--group-justify: flex-end",
    );
  });
});
