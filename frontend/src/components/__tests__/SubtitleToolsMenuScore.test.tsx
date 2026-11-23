/**
 * The tools menu offers a manual score for the subtitle files listed for a
 * movie, episode or sports event.
 */
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import { customRender, screen } from "@/tests";

const file = (type: FormType.ModifySubtitle["type"]) => [
  { type, id: 5, path: "/m/a.en.srt", language: "en" },
];

async function openMenu(menu: React.ReactElement) {
  const user = userEvent.setup();
  customRender(menu);
  await user.click(screen.getByRole("button", { name: "Tools" }));
  return user;
}

describe("SubtitleToolsMenu score item", () => {
  it("offers Set Score for a movie file and opens the score form", async () => {
    const user = await openMenu(
      <SubtitleToolsMenu selections={file("movie")}>
        <button type="button">Tools</button>
      </SubtitleToolsMenu>,
    );

    const item = await screen.findByRole("menuitem", { name: "Set Score..." });
    expect(item).toBeEnabled();
    await user.click(item);

    expect(await screen.findByText("Set Subtitle Score")).toBeInTheDocument();
  });

  it("offers Set Score for an episode file", async () => {
    await openMenu(
      <SubtitleToolsMenu selections={file("episode")}>
        <button type="button">Tools</button>
      </SubtitleToolsMenu>,
    );

    expect(
      await screen.findByRole("menuitem", { name: "Set Score..." }),
    ).toBeEnabled();
  });

  it("offers Set Score for a sports file and opens the score form", async () => {
    const user = await openMenu(
      <SubtitleToolsMenu selections={file("sports")}>
        <button type="button">Tools</button>
      </SubtitleToolsMenu>,
    );

    const item = await screen.findByRole("menuitem", { name: "Set Score..." });
    expect(item).toBeEnabled();
    await user.click(item);
    expect(await screen.findByText("Set Subtitle Score")).toBeInTheDocument();
  });

  it("leaves the item out for a missing subtitle", async () => {
    await openMenu(
      <SubtitleToolsMenu
        selections={[]}
        missingLanguage={{
          code2: "hu",
          name: "Hungarian",
          forced: false,
          hi: false,
          path: null,
        }}
      >
        <button type="button">Tools</button>
      </SubtitleToolsMenu>,
    );

    await screen.findByRole("menuitem", { name: "Sync..." });
    expect(
      screen.queryByRole("menuitem", { name: "Set Score..." }),
    ).not.toBeInTheDocument();
  });

  it.each(["movie", "sports"] as const)(
    "disables the item for an embedded %s track, which has no file to score",
    async (type) => {
      await openMenu(
        <SubtitleToolsMenu
          selections={[{ type, id: 5, path: "", language: "en" }]}
          embeddedTrack={{ codec: "subrip" }}
        >
          <button type="button">Tools</button>
        </SubtitleToolsMenu>,
      );

      expect(
        await screen.findByRole("menuitem", { name: "Set Score..." }),
      ).toBeDisabled();
    },
  );
});
