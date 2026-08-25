import { describe, it } from "vitest";
import { customRender, screen } from "@/tests";
import CombinedSubtitleBadge from "./CombinedSubtitleBadge";

const subtitle = (path: string): Subtitle =>
  ({
    code2: "en",
    name: "English",
    hi: false,
    forced: false,
    path,
    modifier: "combined-hu",
  }) as unknown as Subtitle;

describe("CombinedSubtitleBadge", () => {
  it("renders the language pair the way an ordinary subtitle pill does", () => {
    // The row is read at a glance. A combined entry that renders its languages
    // as bare text breaks the line of pills and reads as a rendering fault, so
    // the pair must be a badge like every other language in the row.
    customRender(<CombinedSubtitleBadge subtitle={subtitle("/a/b.srt")} />);

    const pair = screen.getByText("EN + HU");
    expect(pair.className).toMatch(/badge/i);
  });

  it("marks the container format on a secondary badge", () => {
    customRender(<CombinedSubtitleBadge subtitle={subtitle("/a/b.ass")} />);

    const format = screen.getByText(/combined \(ass\)/i);
    expect(format.className).toMatch(/badge/i);
  });
});
