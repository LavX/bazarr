/* eslint-disable camelcase */

/**
 * Hide track on a sports event's embedded badge, recorded against the
 * event's LOCAL id and its owner.
 */

import { createRef } from "react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type { SportsEvent } from "@/apis/raw/sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SportsTable from "./table";

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
  findTrigger: () => Promise<HTMLElement>,
  label: string,
) {
  const user = userEvent.setup();
  // The row cells are declared inside the table, so a query that settles
  // after the first render can remount them. Find the badge once it is quiet.
  await findTrigger();
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
  await user.click(await findTrigger());
  await user.click(
    await screen.findByRole("menuitem", { name: "Hide track..." }),
  );
  const dialog = within(
    await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
  );
  expect(dialog.getByText(`${label} (embedded track)`)).toBeInTheDocument();
  await user.click(dialog.getByRole("button", { name: "Hide track" }));
}

describe("Hide track on a sports event", () => {
  it("hides a sports track by the event's local id and owner", async () => {
    const posted = capturePosts();
    const event: SportsEvent = {
      id: 61,
      arr_instance_id: 3,
      league_id: 51,
      sportarrEventId: 8,
      file_id: 9,
      partNumber: null,
      partName: null,
      season: 2026,
      episode: 1,
      path: "/sports/event.mkv",
      title: "Card",
      eventDate: null,
      broadcastDate: null,
      hasFile: true,
      monitored: true,
      mapped_path: "/sports/event.mkv",
      file_size: 1,
      profileId: null,
      audio_language: [],
      subtitles: [["fr", null, null]],
      missing_subtitles: [],
    };
    customRender(
      <SportsTable
        // The table opens the newest season through its instance ref.
        ref={createRef()}
        events={[event]}
        onIndex={() => undefined}
        onAllRowsExpandedChanged={() => undefined}
      />,
    );

    await hideThroughMenu(() => screen.findByTitle("Embedded"), "fr");
    await waitFor(() =>
      expect(posted).toEqual([
        {
          media_type: "sports",
          media_id: "61",
          arr_instance_id: "3",
          language: "fr",
          hi: "False",
          forced: "False",
        },
      ]),
    );
  });
});
