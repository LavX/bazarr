import { expect, it, vi } from "vitest";
import { createSubtitleSearchSource } from "@/pages/SubtitleEditor/searchSource";

it("finds literal subtitle text and navigates to the original cue index", () => {
  const select = vi.fn();
  const cues = [
    { id: "first", startMs: 1000, endMs: 2000, text: "Unrelated" },
    {
      id: "second",
      startMs: 84000,
      endMs: 87000,
      text: "There is more to this place [than we know].",
    },
  ];
  const matches = createSubtitleSearchSource(cues, select).search(
    "[THAN WE KNOW]",
  );
  expect(matches).toHaveLength(1);
  expect(matches[0].detail).toBe("00:01:24 · Cue 2");
  matches[0].select();
  expect(select).toHaveBeenCalledWith(1);
  expect(cues[1].text).toBe("There is more to this place [than we know].");
  expect(createSubtitleSearchSource(cues, select).search("   ")).toEqual([]);
});
