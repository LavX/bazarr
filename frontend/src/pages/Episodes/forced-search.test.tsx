/* eslint-disable camelcase */

/**
 * The duplicate-search action on the episodes table: where it shows, the
 * confirm before it sets the flag, and the clear and the badge that stay once
 * a flag exists.
 */

import { useRef } from "react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { Table as TableInstance } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it, vi } from "vitest";
import { useEpisodesBySeriesId } from "@/apis/hooks";
import queryClient from "@/apis/queries";
import type { EmbeddedHiddenTrack } from "@/apis/raw/subtitles";
import { AllProviders } from "@/providers";
import { rawRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import Table from "./table";

const profile = {
  name: "French",
  profileId: 1,
  cutoff: null,
  items: [
    {
      id: 1,
      audio_exclude: "False",
      audio_only_include: "False",
      forced: "False",
      hi: "False",
      language: "fr",
      translate_from: null,
    },
  ],
  mustContain: [],
  mustNotContain: [],
  originalFormat: null,
  tag: undefined,
} as Language.Profile;

const track = (code2: string): Subtitle => ({
  code2,
  name: code2,
  hi: false,
  forced: false,
  path: null,
});

const file = (code2: string): Subtitle => ({
  code2,
  name: code2,
  hi: false,
  forced: false,
  path: `/tv/show/s01e01.${code2}.srt`,
});

function makeEpisode(
  subtitles: Subtitle[],
  extra: Partial<Item.Episode> = {},
): Item.Episode {
  return {
    id: 11,
    series_id: 3,
    sonarrSeriesId: 7,
    sonarrEpisodeId: 101,
    arr_instance_id: 1,
    season: 1,
    episode: 1,
    title: "Episode 1",
    path: "/tv/show/s01e01.mkv",
    monitored: true,
    sceneName: null,
    audio_language: [],
    subtitles,
    missing_subtitles: [],
    ...extra,
  } as unknown as Item.Episode;
}

function hiddenTrack(code2: string): EmbeddedHiddenTrack {
  return {
    media_type: "episode",
    media_id: 11,
    arr_instance_id: 1,
    language: code2,
    code2,
    name: code2,
    hi: false,
    forced: false,
    codec: null,
    hidden_at: null,
  };
}

// Serves the settings, the languages and the hidden tracks, and records the
// flag requests.
function serve({
  embeddedAsDownloaded = true,
  hidden = [],
}: {
  embeddedAsDownloaded?: boolean;
  hidden?: EmbeddedHiddenTrack[];
} = {}) {
  const requests: { method: string; query: Record<string, string> }[] = [];
  const record = (method: string, request: Request) => {
    requests.push({
      method,
      query: Object.fromEntries(new URL(request.url).searchParams.entries()),
    });
    return new HttpResponse(null, { status: 204 });
  };
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto", use_embedded_subs: embeddedAsDownloaded },
      }),
    ),
    http.get("/api/system/languages", () => HttpResponse.json([])),
    http.get("/api/subtitles/embedded-hidden", () =>
      HttpResponse.json({ data: hidden }),
    ),
    http.post("/api/episodes/forced-search", ({ request }) =>
      record("POST", request),
    ),
    http.delete("/api/episodes/forced-search", ({ request }) =>
      record("DELETE", request),
    ),
  );
  return requests;
}

async function mountTable(episode: Item.Episode) {
  // The table opens the newest season through its ref.
  function Harness() {
    const ref = useRef<TableInstance<Item.Episode> | null>(null);
    return (
      <Table
        ref={ref}
        episodes={[episode]}
        profile={profile}
        onAllRowsExpandedChanged={vi.fn()}
      />
    );
  }
  const router = createMemoryRouter([{ path: "/", element: <Harness /> }]);
  rawRender(
    <AllProviders>
      <RouterProvider router={router} />
    </AllProviders>,
  );
  await screen.findByText("Episode 1");
  // Settings, languages and hidden tracks have all arrived.
  await waitFor(() => expect(queryClient.isFetching()).toBe(0));
}

const force = () =>
  screen.queryByRole("button", { name: "Force duplicate search" });
const clear = () =>
  screen.queryByRole("button", { name: "Clear duplicate search" });

describe("Episodes Table, duplicate search action", () => {
  it("shows for an embedded track in a wanted language with the setting on", async () => {
    serve();
    await mountTable(makeEpisode([file("en"), track("fr")]));
    expect(force()).toBeInTheDocument();
    expect(clear()).not.toBeInTheDocument();
  });

  it("is absent with the setting off", async () => {
    serve({ embeddedAsDownloaded: false });
    await mountTable(makeEpisode([track("fr")]));
    expect(force()).not.toBeInTheDocument();
  });

  it("is absent without an embedded track", async () => {
    serve();
    await mountTable(makeEpisode([file("fr")]));
    expect(force()).not.toBeInTheDocument();
  });

  it("is absent when the embedded track is not in a wanted language", async () => {
    serve();
    await mountTable(makeEpisode([track("de")]));
    expect(force()).not.toBeInTheDocument();
  });

  it("shows for a hidden track in a wanted language", async () => {
    serve({ hidden: [hiddenTrack("fr")] });
    await mountTable(makeEpisode([file("en")]));
    await waitFor(() => expect(force()).toBeInTheDocument());
  });

  it("is absent for a hidden track in another language", async () => {
    serve({ hidden: [hiddenTrack("de")] });
    await mountTable(makeEpisode([file("en")]));
    expect(await screen.findByText("Hidden tracks (1)")).toBeInTheDocument();
    expect(force()).not.toBeInTheDocument();
  });

  it("sets the flag only after the confirm", async () => {
    const requests = serve();
    const user = userEvent.setup();
    await mountTable(makeEpisode([track("fr")]));

    await user.click(force()!);
    const dialog = within(
      await screen.findByRole("dialog", {
        name: "Search again for this episode?",
      }),
    );
    expect(requests).toEqual([]);

    await user.click(dialog.getByRole("button", { name: "Search again" }));
    await waitFor(() =>
      expect(requests).toEqual([
        {
          method: "POST",
          query: { seriesid: "7", episodeid: "101", arr_instance_id: "1" },
        },
      ]),
    );
  });

  it("sets nothing when the confirm is cancelled", async () => {
    const requests = serve();
    const user = userEvent.setup();
    await mountTable(makeEpisode([track("fr")]));

    await user.click(force()!);
    const dialog = within(
      await screen.findByRole("dialog", {
        name: "Search again for this episode?",
      }),
    );
    await user.click(dialog.getByRole("button", { name: "Cancel" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("dialog", {
          name: "Search again for this episode?",
        }),
      ).not.toBeInTheDocument(),
    );
    expect(requests).toEqual([]);
  });

  it("clears the flag in the same slot with no confirm", async () => {
    const requests = serve();
    const user = userEvent.setup();
    await mountTable(
      makeEpisode([track("fr")], { forced_search_pending: true }),
    );

    expect(force()).not.toBeInTheDocument();
    expect(screen.getByText("Duplicate search pending")).toBeInTheDocument();
    await user.click(clear()!);

    await waitFor(() =>
      expect(requests).toEqual([
        {
          method: "DELETE",
          query: { seriesid: "7", episodeid: "101", arr_instance_id: "1" },
        },
      ]),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("keeps the badge and the clear after the setting and the track are gone", async () => {
    serve({ embeddedAsDownloaded: false });
    await mountTable(
      makeEpisode([file("en")], { forced_search_pending: true }),
    );

    expect(screen.getByText("Duplicate search pending")).toBeInTheDocument();
    expect(clear()).toBeInTheDocument();
    expect(force()).not.toBeInTheDocument();
  });

  it("shows no badge on an episode without a flag", async () => {
    serve();
    await mountTable(makeEpisode([track("fr")]));
    expect(
      screen.queryByText("Duplicate search pending"),
    ).not.toBeInTheDocument();
  });

  it("shows the badge after a set and drops it after a clear, without a reload", async () => {
    // A backend that keeps the flag, so the list the page refetches changes.
    let pending = false;
    serve();
    server.use(
      http.get("/api/episodes", () =>
        HttpResponse.json({
          data: [
            makeEpisode([track("fr")], { forced_search_pending: pending }),
          ],
        }),
      ),
      http.post("/api/episodes/forced-search", () => {
        pending = true;
        return new HttpResponse(null, { status: 204 });
      }),
      http.delete("/api/episodes/forced-search", () => {
        pending = false;
        return new HttpResponse(null, { status: 204 });
      }),
    );

    function Page() {
      const ref = useRef<TableInstance<Item.Episode> | null>(null);
      const { data } = useEpisodesBySeriesId(3);
      return (
        <Table
          ref={ref}
          episodes={data ?? null}
          profile={profile}
          onAllRowsExpandedChanged={vi.fn()}
        />
      );
    }
    const router = createMemoryRouter([{ path: "/", element: <Page /> }]);
    rawRender(
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>,
    );
    const user = userEvent.setup();

    await user.click(
      await screen.findByRole("button", { name: "Force duplicate search" }),
    );
    await user.click(
      within(
        await screen.findByRole("dialog", {
          name: "Search again for this episode?",
        }),
      ).getByRole("button", { name: "Search again" }),
    );
    expect(
      await screen.findByText("Duplicate search pending"),
    ).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Clear duplicate search" }),
    );
    await waitFor(() =>
      expect(
        screen.queryByText("Duplicate search pending"),
      ).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByRole("button", { name: "Force duplicate search" }),
    ).toBeInTheDocument();
  });

  it("explains the pending badge to a keyboard user", async () => {
    serve();
    await mountTable(
      makeEpisode([track("fr")], { forced_search_pending: true }),
    );

    // Tab to the badge: it is in the tab order.
    const user = userEvent.setup();
    for (let i = 0; i < 20; i += 1) {
      if (document.activeElement?.textContent === "Duplicate search pending") {
        break;
      }
      await user.tab();
    }
    expect(document.activeElement?.textContent).toBe(
      "Duplicate search pending",
    );
    expect(
      await screen.findByText(/ignores this episode's embedded subtitles/),
    ).toBeInTheDocument();
  });
});
