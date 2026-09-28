/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";
import PlayOnMenu, { NO_CANDIDATE } from "./PlayOnMenu";

const ROOT = "/api/system/media-server-links";
const EMBY = { instance_id: "e1", kind: "emby", display_name: "Emby" };
const PLEX = { instance_id: "p1", kind: "plex", display_name: "Plex" };
const SILO = { instance_id: "s1", kind: "silo", display_name: "Silo" };

type Answer = { state: string; url: string | null };

function serve(
  destinations: object[],
  candidates: object[],
  answers: Record<string, Answer | (() => Promise<Answer>)>,
) {
  const asked: string[] = [];
  server.use(
    http.get(`${ROOT}/destinations`, () =>
      HttpResponse.json({ data: destinations }),
    ),
    http.get(`${ROOT}/candidates`, ({ request }) => {
      asked.push(`candidates:${new URL(request.url).search}`);
      return HttpResponse.json({ data: candidates });
    }),
    http.get(`${ROOT}/resolve`, async ({ request }) => {
      const instance = new URL(request.url).searchParams.get("instance_id")!;
      asked.push(`resolve:${instance}`);
      const answer = answers[instance];
      const body = typeof answer === "function" ? await answer() : answer;
      return HttpResponse.json({ instance_id: instance, ...body });
    }),
  );
  return asked;
}

describe("PlayOnMenu", () => {
  it("renders nothing and asks nothing more when no destination is configured", async () => {
    const asked = serve([], [], {});
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /Play/ })).toBeNull(),
    );
    expect(asked).toEqual([]);
  });

  it("asks nothing until the pointer rests, then fills stable rows in place", async () => {
    let finishPlex: (() => void) | undefined;
    const asked = serve([EMBY, PLEX, SILO], [EMBY, PLEX, SILO], {
      e1: {
        state: "resolved",
        url: "https://emby.example.com/web/index.html#!/item?id=1",
      },
      p1: () =>
        new Promise((resolve) => {
          finishPlex = () => resolve({ state: "unreachable", url: null });
        }),
      s1: { state: "not_found", url: null },
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="episode"
        id={9}
        itemLabel="S01E02"
        variant="action"
      />,
    );
    const trigger = await screen.findByRole("button", {
      name: "Play S01E02 on a media server",
    });
    expect(asked).toEqual([]);
    await user.click(trigger);
    // Mantine hides a floating dropdown once jsdom reports its target has no
    // size, so the rows are found with hidden: true, as the other menu tests do.
    const row = (name: string) =>
      screen.findByRole("menuitem", { name, hidden: true });
    const emby = await row("Play on Emby");
    expect(emby).toHaveAttribute(
      "href",
      "https://emby.example.com/web/index.html#!/item?id=1",
    );
    expect(emby).toHaveAttribute("target", "_blank");
    expect(emby).toHaveAttribute("rel", "noopener noreferrer");
    expect(await row("Not found on Silo")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
    const rows = () =>
      screen
        .getAllByRole("menuitem", { hidden: true })
        .map((item) => item.textContent);
    expect(rows()).toEqual([
      "Play on Emby",
      "Checking Plex",
      "Not found on Silo",
    ]);
    const plex = screen.getByRole("menuitem", {
      name: "Checking Plex",
      hidden: true,
    });
    finishPlex?.();
    await row("Plex is unreachable");
    // The same element, answered in place.
    expect(plex).toHaveTextContent("Plex is unreachable");
    expect(plex).not.toHaveAttribute("href");
    expect(rows()).toEqual([
      "Play on Emby",
      "Plex is unreachable",
      "Not found on Silo",
    ]);
    expect(asked).toEqual([
      "candidates:?media_type=episode&id=9",
      "resolve:e1",
      "resolve:p1",
      "resolve:s1",
    ]);
  });

  it("says so when no destination can hold the file", async () => {
    serve([EMBY, PLEX], [], {});
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    await user.click(
      await screen.findByRole("button", {
        name: "Play Metropolis on a media server",
      }),
    );
    expect(await screen.findByText(NO_CANDIDATE)).toBeInTheDocument();
  });

  it("is the link itself when one destination is configured, and keeps focus while it resolves", async () => {
    serve([SILO], [SILO], {
      s1: { state: "resolved", url: "http://silo.lan:8080/item/movie-tmdb-19" },
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    const link = await screen.findByLabelText("Play Metropolis on Silo");
    expect(link.tagName).toBe("A");
    expect(link).toHaveAttribute("aria-disabled", "true");
    expect(link).not.toHaveAttribute("href");
    await user.tab();
    expect(link).toHaveFocus();
    await waitFor(() =>
      expect(link).toHaveAttribute(
        "href",
        "http://silo.lan:8080/item/movie-tmdb-19",
      ),
    );
    expect(link).toHaveFocus();
    expect(link).not.toHaveAttribute("aria-disabled");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
  });

  it("never turns a non-web address into a link", async () => {
    serve([SILO], [SILO], {
      s1: { state: "resolved", url: "javascript:alert(1)" },
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="action"
      />,
    );
    const link = await screen.findByLabelText("Play Metropolis on Silo");
    await user.tab();
    await screen.findByText("Silo is unreachable");
    expect(link).not.toHaveAttribute("href");
  });

  it("asks once the pointer rests, never for a pointer passing over, and only once", async () => {
    const asked = serve([EMBY, PLEX], [EMBY, PLEX], {
      e1: {
        state: "resolved",
        url: "https://emby.example.com/web/index.html#!/item?id=1",
      },
      p1: { state: "not_found", url: null },
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    const trigger = await screen.findByRole("button", {
      name: "Play Metropolis on a media server",
    });
    const settle = () => new Promise((resolve) => setTimeout(resolve, 400));
    await user.hover(trigger);
    await user.unhover(trigger);
    await settle();
    expect(asked).toEqual([]);
    await user.hover(trigger);
    await waitFor(() => expect(asked).toHaveLength(3));
    await user.unhover(trigger);
    await user.hover(trigger);
    await settle();
    expect(asked).toEqual([
      "candidates:?media_type=movie&id=1",
      "resolve:e1",
      "resolve:p1",
    ]);
  });

  it("asks a server that did not answer again the next time the menu opens", async () => {
    let plex: Answer = { state: "unreachable", url: null };
    const asked = serve([EMBY, PLEX], [EMBY, PLEX], {
      e1: {
        state: "resolved",
        url: "https://emby.example.com/web/index.html#!/item?id=1",
      },
      p1: async () => plex,
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    const trigger = await screen.findByRole("button", {
      name: "Play Metropolis on a media server",
    });
    await user.click(trigger);
    await screen.findByRole("menuitem", {
      name: "Plex is unreachable",
      hidden: true,
    });
    await user.click(trigger);
    await waitFor(() =>
      expect(trigger).toHaveAttribute("aria-expanded", "false"),
    );
    plex = {
      state: "resolved",
      url: "https://app.plex.tv/desktop/#!/server/m1/details?key=1",
    };
    await user.click(trigger);
    expect(
      await screen.findByRole("menuitem", {
        name: "Play on Plex",
        hidden: true,
      }),
    ).toHaveAttribute(
      "href",
      "https://app.plex.tv/desktop/#!/server/m1/details?key=1",
    );
    // Emby's answer was kept; only the server that did not answer was asked.
    expect(asked).toEqual([
      "candidates:?media_type=movie&id=1",
      "resolve:e1",
      "resolve:p1",
      "resolve:p1",
    ]);
  });

  it("asks the one destination again when its link takes focus after no answer", async () => {
    let silo: Answer = { state: "unreachable", url: null };
    const asked = serve([SILO], [SILO], { s1: async () => silo });
    const user = userEvent.setup();
    customRender(
      <>
        <PlayOnMenu
          mediaType="movie"
          id={1}
          itemLabel="Metropolis"
          variant="toolbox"
        />
        <button type="button">Elsewhere</button>
      </>,
    );
    const link = await screen.findByLabelText("Play Metropolis on Silo");
    await user.tab();
    await screen.findByText("Silo is unreachable");
    await user.tab();
    silo = {
      state: "resolved",
      url: "http://silo.lan:8080/item/movie-tmdb-19",
    };
    await user.tab({ shift: true });
    await waitFor(() =>
      expect(link).toHaveAttribute(
        "href",
        "http://silo.lan:8080/item/movie-tmdb-19",
      ),
    );
    expect(link).toHaveFocus();
    expect(asked).toEqual([
      "candidates:?media_type=movie&id=1",
      "resolve:s1",
      "resolve:s1",
    ]);
  });

  it("reaches every row with the arrow keys, answered or still checking", async () => {
    let finishPlex: (() => void) | undefined;
    serve([EMBY, PLEX, SILO], [EMBY, PLEX, SILO], {
      e1: {
        state: "resolved",
        url: "https://emby.example.com/web/index.html#!/item?id=1",
      },
      p1: () =>
        new Promise((resolve) => {
          finishPlex = () => resolve({ state: "not_found", url: null });
        }),
      s1: { state: "not_found", url: null },
    });
    const user = userEvent.setup();
    customRender(
      <PlayOnMenu
        mediaType="movie"
        id={1}
        itemLabel="Metropolis"
        variant="toolbox"
      />,
    );
    await screen.findByRole("button", {
      name: "Play Metropolis on a media server",
    });
    await user.tab();
    await user.keyboard("{Enter}");
    await screen.findByRole("menuitem", {
      name: "Not found on Silo",
      hidden: true,
    });
    // Three presses reach all three rows, whether or not opening the menu
    // already focused the first one: the rows loop.
    const reached = new Set<string | null | undefined>();
    for (let step = 0; step < 3; step += 1) {
      await user.keyboard("{ArrowDown}");
      reached.add(document.activeElement?.textContent);
    }
    expect(reached).toEqual(
      new Set(["Play on Emby", "Checking Plex", "Not found on Silo"]),
    );
    finishPlex?.();
  });
});
