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
        embeddedTrack
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
        embeddedTrack
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
