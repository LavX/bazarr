/**
 * A subtitle tool is sent to the backend as soon as it is picked.
 *
 * The backend queues each one as a job and answers at once, so there is no
 * client-side queue in between any more: two selections are two requests,
 * made immediately, one per selection.
 */

import { Button } from "@mantine/core";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import api from "@/apis/raw";
import { customRender, screen, waitFor, within } from "@/tests";
import SubtitleToolsMenu from "./SubtitleToolsMenu";

vi.mock("@/apis/raw", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/apis/raw")>();
  return {
    default: {
      ...actual.default,
      subtitles: {
        ...actual.default.subtitles,
        // eslint-disable-next-line camelcase
        modify: vi.fn().mockResolvedValue({ job_id: 4 }),
        extractEmbedded: vi
          .fn()
          // eslint-disable-next-line camelcase
          .mockResolvedValue({ job_id: 5, path: "/m/a.en.srt" }),
      },
    },
  };
});

function selection(path: string): FormType.ModifySubtitle {
  return {
    id: 5,
    type: "movie",
    language: "en",
    path,
    hi: "False",
    forced: "False",
    // eslint-disable-next-line camelcase
    arr_instance_id: 2,
  } as FormType.ModifySubtitle;
}

describe("SubtitleToolsMenu", () => {
  beforeEach(() => {
    vi.mocked(api.subtitles.modify).mockClear();
  });

  it("sends one request per selection as soon as a tool is picked", async () => {
    const user = userEvent.setup();
    customRender(
      <SubtitleToolsMenu
        selections={[selection("/m/a.en.srt"), selection("/m/b.en.srt")]}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );

    await user.click(screen.getByRole("button", { name: "Tools" }));
    await user.click(await screen.findByText("Remove HI Tags"));

    await waitFor(() => expect(api.subtitles.modify).toHaveBeenCalledTimes(2));
    expect(
      vi.mocked(api.subtitles.modify).mock.calls.map((call) => call[0]),
    ).toEqual(["remove_HI", "remove_HI"]);
    expect(
      vi.mocked(api.subtitles.modify).mock.calls.map((call) => call[1].path),
    ).toEqual(["/m/a.en.srt", "/m/b.en.srt"]);
  });
});

describe("SubtitleToolsMenu red slot", () => {
  it("hides an embedded track instead of deleting it, with its own confirm copy", async () => {
    const user = userEvent.setup();
    const onAction = vi.fn();
    customRender(
      <SubtitleToolsMenu
        selections={[selection("")]}
        embeddedTrack={{ codec: "subrip" }}
        trackLabel="English (HI)"
        onAction={onAction}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );

    await user.click(screen.getByRole("button", { name: "Tools" }));
    expect(
      screen.queryByRole("menuitem", { name: "Delete..." }),
    ).not.toBeInTheDocument();
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );

    const dialog = within(
      await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
    );
    expect(
      dialog.getByText(
        "Bazarr+ will stop listing this track for this title and will treat English (HI) as wanted again. The media file is not changed.",
      ),
    ).toBeInTheDocument();
    // The list names the track, never a path: an embedded track has none.
    expect(
      dialog.getByText("English (HI) (embedded track)"),
    ).toBeInTheDocument();
    await user.click(dialog.getByRole("button", { name: "Hide track" }));
    expect(onAction).toHaveBeenCalledWith("hide");
  });

  it("lists every track one Hide takes when several share the key", async () => {
    const user = userEvent.setup();
    customRender(
      <SubtitleToolsMenu
        selections={[selection("")]}
        embeddedTrack={{ codec: "subrip" }}
        trackLabel="English"
        coveredTracks={["English", "English"]}
        onAction={vi.fn()}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );

    await user.click(screen.getByRole("button", { name: "Tools" }));
    await user.click(
      await screen.findByRole("menuitem", { name: "Hide track..." }),
    );
    const dialog = within(
      await screen.findByRole("dialog", { name: "Hide this embedded track?" }),
    );
    expect(dialog.getAllByText("English (embedded track)")).toHaveLength(2);
    expect(
      dialog.getByText(
        "Bazarr+ tells embedded tracks apart only by language, HI and forced, so these 2 tracks are hidden together.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps Delete, and no Hide, on a subtitle file", async () => {
    const user = userEvent.setup();
    customRender(
      <SubtitleToolsMenu
        selections={[selection("/m/a.en.srt")]}
        onAction={vi.fn()}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );

    await user.click(screen.getByRole("button", { name: "Tools" }));
    expect(
      await screen.findByRole("menuitem", { name: "Delete..." }),
    ).toBeEnabled();
    expect(
      screen.queryByRole("menuitem", { name: "Hide track..." }),
    ).not.toBeInTheDocument();
  });
});

describe("SubtitleToolsMenu modes", () => {
  const TOOLS = [
    "Sync...",
    "Change Frame Rate...",
    "Adjust Times...",
    "Remove HI Tags",
    "Remove Style Tags",
    "Remove Emoji",
    "OCR Fixes",
    "Common Fixes",
    "Fix Uppercase",
    "Reverse RTL",
    "Add Color...",
    "Translate...",
  ];

  async function open(props: Partial<Parameters<typeof SubtitleToolsMenu>[0]>) {
    const user = userEvent.setup();
    customRender(
      <SubtitleToolsMenu
        selections={[selection("")]}
        onAction={vi.fn()}
        {...props}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );
    await user.click(screen.getByRole("button", { name: "Tools" }));
    await screen.findByText("Remove HI Tags");
    return user;
  }

  // The dropdown can already be closing by the time the assertions run, so
  // items are looked up whether or not they are still visible: what is
  // checked is what the menu rendered, enabled or not.
  const item = (name: string) =>
    screen.getByRole("menuitem", { name, hidden: true });
  const noItem = (name: string) =>
    screen.queryByRole("menuitem", { name, hidden: true });

  it("external file: every tool, no Extract to file, Delete", async () => {
    await open({ selections: [selection("/m/a.en.srt")] });

    for (const name of [
      ...TOOLS,
      "Two-Point Fit...",
      "View",
      "Edit",
      "Download",
    ]) {
      expect(item(name)).toBeEnabled();
    }
    expect(noItem("Extract to file")).not.toBeInTheDocument();
    expect(item("Delete...")).toBeEnabled();
  });

  it.each([["subrip"], [null]])(
    "embedded text track (codec %s): every tool but Two-Point Fit, Extract to file, Hide",
    async (codec) => {
      await open({ embeddedTrack: { codec } });

      for (const name of TOOLS) {
        expect(item(name)).toBeEnabled();
      }
      // Its lines are picked from a file, so it waits for Extract to file.
      expect(item("Two-Point Fit...")).toBeDisabled();
      expect(item("Extract to file")).toBeEnabled();
      // They read the track out of the video; nothing is written until Save.
      for (const name of ["View", "Edit", "Download"]) {
        expect(item(name)).toBeEnabled();
      }
      expect(item("Search")).toBeDisabled();
      expect(item("Hide track...")).toBeEnabled();
      expect(noItem("Delete...")).not.toBeInTheDocument();
      expect(screen.queryByText(/Image-based track/)).not.toBeInTheDocument();
    },
  );

  it.each([
    ["pgs", "PGS"],
    ["vobsub", "VobSub"],
    ["dvbsub", "DVB"],
  ])(
    "embedded image-based track (%s): Extract to file and Hide only",
    async (codec, label) => {
      await open({ embeddedTrack: { codec } });

      expect(
        screen.getByText(
          `Image-based track (${label}): the tools, View, Edit and Download need text. Extract to file copies it out as it is, for players.`,
        ),
      ).toBeInTheDocument();
      for (const name of [
        ...TOOLS,
        "Two-Point Fit...",
        "View",
        "Edit",
        "Download",
        "Search",
      ]) {
        expect(item(name)).toBeDisabled();
      }
      expect(item("Extract to file")).toBeEnabled();
      expect(item("Hide track...")).toBeEnabled();
    },
  );

  it("a tool on an embedded row sends the row's codec", async () => {
    vi.mocked(api.subtitles.modify).mockClear();
    const user = await open({ embeddedTrack: { codec: "ass" } });

    await user.click(item("Remove HI Tags"));

    await waitFor(() => expect(api.subtitles.modify).toHaveBeenCalledTimes(1));
    expect(vi.mocked(api.subtitles.modify).mock.calls[0]).toEqual([
      "remove_HI",
      expect.objectContaining({ path: "", language: "en", codec: "ass" }),
    ]);
  });

  it("Extract to file sends the row with its codec", async () => {
    vi.mocked(api.subtitles.extractEmbedded).mockClear();
    const user = await open({ embeddedTrack: { codec: "pgs" } });

    await user.click(item("Extract to file"));

    await waitFor(() =>
      expect(api.subtitles.extractEmbedded).toHaveBeenCalledTimes(1),
    );
    expect(
      vi.mocked(api.subtitles.extractEmbedded).mock.calls[0][0],
    ).toMatchObject({
      id: 5,
      type: "movie",
      language: "en",
      path: "",
      codec: "pgs",
    });
  });
});

describe("SubtitleToolsMenu, View, Edit and Download on an embedded track", () => {
  // Mantine hides a floating dropdown once jsdom reports its target has no
  // size, so the rows are found with hidden: true, as the other menu tests do.
  const item = (name: string) =>
    screen.findByRole("menuitem", { name, hidden: true });

  function showEmbedded(codec: string | null, onAction = vi.fn()) {
    customRender(
      <SubtitleToolsMenu
        selections={[selection("")]}
        embeddedTrack={{ codec }}
        trackLabel="English"
        onAction={onAction}
      >
        <Button>Tools</Button>
      </SubtitleToolsMenu>,
    );
    return onAction;
  }

  it.each(["View", "Edit", "Download"])(
    "hands %s on a text track to the row",
    async (name) => {
      const user = userEvent.setup();
      const onAction = showEmbedded("ass");

      await user.click(screen.getByRole("button", { name: "Tools" }));
      await user.click(await item(name));

      expect(onAction).toHaveBeenCalledWith(name.toLowerCase());
    },
  );

  it("explains an image-based track in one line", async () => {
    const user = userEvent.setup();
    showEmbedded("pgs");

    await user.click(screen.getByRole("button", { name: "Tools" }));
    await item("View");

    expect(screen.getAllByText(/Image-based track/)).toHaveLength(1);
  });
});
