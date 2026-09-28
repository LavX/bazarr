/* eslint-disable camelcase */

/**
 * The hidden-tracks disclosure of a row another tab or user changed. The
 * socket event a hide's re-index sends refreshes the row, so the track
 * leaves the table; the disclosure that explains and undoes that must
 * refresh with it rather than wait for its cache to go stale.
 */

import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type {
  EmbeddedHiddenTrack,
  HiddenTrackMediaType,
} from "@/apis/raw/subtitles";
import { createDefaultReducer } from "@/modules/socketio/reducer";
import { customRender, screen, waitFor } from "@/tests";
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
