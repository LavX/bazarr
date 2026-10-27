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
        ocrEmbedded: vi
          .fn()
          // eslint-disable-next-line camelcase
          .mockResolvedValue({ job_id: 6, path: "/m/a.en.srt" }),
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
    [
      "pgs",
      "Image-based track (PGS): the tools, Translate, View, Edit and Download need text. OCR to text reads it into an SRT; Extract to file copies it out as it is, for players.",
    ],
    [
      "vobsub",
      "Image-based track (VobSub): the tools, Translate, View, Edit and Download need text. OCR to text reads it into an SRT; Extract to file copies it out as it is, for players.",
    ],
    [
      "dvbsub",
      "Image-based track (DVB): the tools, Translate, View, Edit and Download need text, and OCR cannot read DVB tracks. Extract to file copies it out as it is, for players.",
    ],
  ])(
    "embedded image-based track (%s): OCR where it can read it, Extract to file and Hide",
    async (codec, note) => {
      await open({ embeddedTrack: { codec } });

      expect(screen.getByText(note)).toBeInTheDocument();
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

describe("SubtitleToolsMenu OCR to text", () => {
  const OCR_TOOLTIP =
    "Reads the image subtitles with Tesseract and writes an SRT next to the video. Italics are not preserved.";

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

  const menuItem = (name: string | RegExp) =>
    screen.getByRole("menuitem", { name, hidden: true });
  const noItem = (name: string) =>
    screen.queryByRole("menuitem", { name, hidden: true });

  beforeEach(() => {
    vi.mocked(api.subtitles.ocrEmbedded).mockClear();
  });

  it.each([["pgs"], ["vobsub"], ["hdmv_pgs_subtitle"], ["dvd_subtitle"]])(
    "is offered on a %s row, above Extract to file",
    async (codec) => {
      await open({ embeddedTrack: { codec } });

      const ocr = menuItem("OCR to text...");
      expect(ocr).toBeEnabled();
      expect(
        ocr.compareDocumentPosition(menuItem("Extract to file")) &
          Node.DOCUMENT_POSITION_FOLLOWING,
      ).toBeTruthy();
    },
  );

  it.each([
    ["DVB", { embeddedTrack: { codec: "dvbsub" } }],
    ["an unknown codec", { embeddedTrack: { codec: null } }],
    ["a text track", { embeddedTrack: { codec: "subrip" } }],
    ["an external file", { selections: [selection("/m/a.en.srt")] }],
  ])("is absent on %s", async (_kind, props) => {
    await open(props);

    expect(noItem("OCR to text...")).not.toBeInTheDocument();
  });

  it("explains itself on hover", async () => {
    const user = await open({ embeddedTrack: { codec: "pgs" } });

    await user.hover(menuItem("OCR to text..."));

    expect(await screen.findByText(OCR_TOOLTIP)).toBeInTheDocument();
  });

  it("queues the job for the row with its codec, with no confirmation", async () => {
    const user = await open({ embeddedTrack: { codec: "vobsub" } });

    await user.click(menuItem("OCR to text..."));

    await waitFor(() =>
      expect(api.subtitles.ocrEmbedded).toHaveBeenCalledTimes(1),
    );
    expect(vi.mocked(api.subtitles.ocrEmbedded).mock.calls[0][0]).toMatchObject(
      {
        id: 5,
        type: "movie",
        language: "en",
        path: "",
        codec: "vobsub",
      },
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(
      await screen.findByText(
        "Reading the track into a.en.srt. Follow it in Jobs.",
      ),
    ).toBeInTheDocument();
  });

  describe("Translate from an embedded source", () => {
    const NOTE =
      "Reads the track with OCR first and saves the result as a subtitle file in your subtitle folder, named like downloaded subtitles, then translates it.";
    const source = (codec: string | null, extra: Partial<Subtitle> = {}) =>
      ({
        code2: "en",
        name: "English",
        path: null,
        hi: false,
        forced: false,
        codec,
        ...extra,
      }) as Subtitle;

    async function openMissing(
      sources: Subtitle[],
      mediaType: "movie" | "episode" | "sports" = "movie",
    ) {
      const user = userEvent.setup();
      customRender(
        <SubtitleToolsMenu
          selections={[]}
          missingLanguage={{
            code2: "hu",
            name: "Hungarian",
            hi: false,
            forced: false,
            path: null,
          }}
          translationSources={sources}
          mediaId={5}
          mediaType={mediaType}
          onAction={vi.fn()}
        >
          <Button>Tools</Button>
        </SubtitleToolsMenu>,
      );
      await user.click(screen.getByRole("button", { name: "Tools" }));
      await screen.findByText("Remove HI Tags");
      return user;
    }

    beforeEach(() => {
      vi.mocked(api.subtitles.modify).mockClear();
    });

    it.each([
      ["pgs", "PGS"],
      ["vobsub", "VobSub"],
    ])(
      "is enabled on a %s source and says it reads the track with OCR first",
      async (codec, label) => {
        await openMissing([source(codec)]);

        const item = menuItem(
          `Translate from English (embedded ${label}, read with OCR) ${NOTE}`,
        );
        expect(item).toBeEnabled();
      },
    );

    it.each([["hdmv_pgs_subtitle"], ["dvd_subtitle"], ["dvd"]])(
      "reads a %s source with OCR as well",
      async (codec) => {
        await openMissing([source(codec)]);

        expect(
          menuItem(/Translate from English \(embedded .*, read with OCR\)/),
        ).toBeEnabled();
      },
    );

    it("does not promise a file name or a folder the settings may change", async () => {
      // The name takes the HI Extension setting (hi, sdh, cc), the language's
      // own file code and forced over HI, and the folder is the configured
      // subtitle folder, so the note names none of them.
      await openMissing([
        source("pgs", { hi: true }),
        source("vobsub", { forced: true }),
        source("pgs"),
      ]);

      expect(screen.getAllByText(NOTE)).toHaveLength(3);
      expect(screen.queryByText(/<video>/)).not.toBeInTheDocument();
      expect(screen.queryByText(/\.hi\b/)).not.toBeInTheDocument();
      expect(screen.queryByText(/next to the video/)).not.toBeInTheDocument();
    });

    it("keeps DVB disabled, with no text to read", async () => {
      await openMissing([source("dvbsub")]);

      expect(
        menuItem("Translate from English (embedded DVB, no text)"),
      ).toBeDisabled();
    });

    it("leaves a text or unknown source as it was, with no OCR note", async () => {
      await openMissing([source("subrip"), source(null, { hi: true })]);

      const items = screen.getAllByRole("menuitem", {
        name: /Translate from English \(embedded\)/,
        hidden: true,
      });
      expect(items).toHaveLength(2);
      items.forEach((item) => expect(item).toBeEnabled());
      expect(screen.queryByText(/Reads the track with OCR/)).toBeNull();
    });

    it("disables an image source on a sports event and points to OCR to text", async () => {
      await openMissing([source("pgs")], "sports");

      const item = menuItem(/Translate from English \(embedded PGS/);
      expect(item).toBeDisabled();
      expect(
        screen.getByText(
          "Not available for sports events. Run OCR to text on this track first, then translate the file it writes.",
        ),
      ).toBeInTheDocument();
    });

    it("sends the source row's codec with the request", async () => {
      const user = await openMissing([source("pgs"), source("subrip")]);

      await user.click(menuItem(/embedded PGS, read with OCR/));

      await waitFor(() =>
        expect(api.subtitles.modify).toHaveBeenCalledTimes(1),
      );
      const [action, form] = vi.mocked(api.subtitles.modify).mock.calls[0];
      expect(action).toBe("translate");
      expect(form).toMatchObject({
        id: 5,
        type: "movie",
        language: "hu",
        path: "",
        // eslint-disable-next-line camelcase
        from_language: "en",
        codec: "pgs",
      });
    });

    it("sends no codec for a source row whose format is not known", async () => {
      const user = await openMissing([source(null)]);

      await user.click(menuItem(/Translate from English \(embedded\)/));

      await waitFor(() =>
        expect(api.subtitles.modify).toHaveBeenCalledTimes(1),
      );
      const form = vi.mocked(api.subtitles.modify).mock.calls[0][1];
      expect(form.codec).toBeUndefined();
    });
  });
});
