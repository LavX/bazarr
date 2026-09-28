/* eslint-disable camelcase */

/**
 * Hide track on an episode's embedded badge. It is recorded against the
 * episode's LOCAL id, never its sonarrEpisodeId.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { Subtitle } from "./components";

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

function capturePosts() {
  const posted: Record<string, string>[] = [];
  server.use(
    http.post("/api/subtitles/embedded-hidden", async ({ request }) => {
      const form = Object.fromEntries(
        (await request.formData()).entries(),
      ) as Record<string, string>;
      posted.push(form);
      return wantedAgain(form);
    }),
  );
  return posted;
}

async function hideThroughMenu(
  trigger: HTMLElement,
  label: string,
  tracks = 1,
) {
  const user = userEvent.setup();
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  await user.click(trigger);
  await user.click(
    await screen.findByRole("menuitem", { name: "Hide track..." }),
  );
  const dialog = within(
    await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
  );
  expect(dialog.getAllByText(`${label} (embedded track)`)).toHaveLength(tracks);
  await user.click(dialog.getByRole("button", { name: "Hide track" }));
}

describe("Hide track on an episode", () => {
  it("hides an episode track by the episode's local id", async () => {
    const posted = capturePosts();
    customRender(
      <Subtitle
        seriesId={5}
        episodeId={51}
        localEpisodeId={2}
        arrInstanceId={1}
        subtitle={{
          code2: "fr",
          name: "French",
          hi: false,
          forced: true,
          path: null,
        }}
        availableSubtitles={[]}
      />,
    );

    await hideThroughMenu(
      await screen.findByTitle("Embedded in the video file"),
      "French (forced)",
    );
    await waitFor(() =>
      expect(posted).toEqual([
        {
          media_type: "episode",
          media_id: "2",
          arr_instance_id: "1",
          language: "fr",
          hi: "False",
          forced: "True",
        },
      ]),
    );
  });

  it("lists both tracks when an SRT and a PGS French share the key", async () => {
    const posted = capturePosts();
    const french: Subtitle = {
      code2: "fr",
      name: "French",
      hi: false,
      forced: false,
      path: null,
    };
    customRender(
      <Subtitle
        seriesId={5}
        episodeId={51}
        localEpisodeId={2}
        arrInstanceId={1}
        subtitle={french}
        availableSubtitles={[
          french,
          { ...french },
          { ...french, path: "/tv/s/e02.fr.srt" },
        ]}
      />,
    );

    await hideThroughMenu(
      await screen.findByTitle("Embedded in the video file"),
      "French",
      2,
    );
    await waitFor(() => expect(posted).toHaveLength(1));
  });
});
