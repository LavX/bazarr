import { describe, expect, it } from "vitest";
import { customRender, screen } from "@/tests";
import { Subtitle } from "./components";

const track = (codec?: string | null) =>
  ({
    name: "English",
    code2: "en",
    code3: "eng",
    path: null,
    forced: false,
    hi: false,
    codec,
  }) as Subtitle;

const renderBadge = (subtitle: Subtitle) =>
  customRender(
    <Subtitle
      seriesId={1}
      episodeId={2}
      localEpisodeId={20}
      arrInstanceId={1}
      subtitle={subtitle}
      availableSubtitles={[]}
    />,
  );

describe("Episode embedded track codec", () => {
  it("names a bitmap codec on hover", async () => {
    renderBadge(track("vobsub"));
    expect(
      await screen.findByTitle(
        "Embedded in the video file (VobSub, image-based)",
      ),
    ).toHaveTextContent(/^en$/i);
  });

  it("names a text codec on hover", async () => {
    renderBadge(track("subrip"));
    expect(
      await screen.findByTitle("Embedded in the video file (SRT)"),
    ).toHaveTextContent(/^en$/i);
  });

  it("keeps the old title when the codec is unknown", async () => {
    renderBadge(track(null));
    expect(
      await screen.findByTitle("Embedded in the video file"),
    ).toHaveTextContent(/^en$/i);
  });
});
