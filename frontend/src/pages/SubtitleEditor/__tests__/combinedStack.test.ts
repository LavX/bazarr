import { describe, expect, it } from "vitest";
import {
  isCombinedAssSubtitle,
  mergeCombinedAssCues,
  splitCombinedAssCues,
} from "@/pages/SubtitleEditor/combinedStack";
import type { Cue } from "@/pages/SubtitleEditor/types";

// Two positioned dialogues at the same timestamp, as the ASS parser would
// import a combined cue: primary (Bottom) first, then secondary (Top).
function combinedAssCues(): Cue[] {
  return [
    {
      id: "a",
      startMs: 1000,
      endMs: 2000,
      text: "Here you are, sir.\nMain level, please.",
      rawText:
        "Dialogue: 0,0:00:01.00,0:00:02.00,Bottom,,0,0,0,,Here you are, sir.\\NMain level, please.",
    },
    {
      id: "b",
      startMs: 1000,
      endMs: 2000,
      text: "Megérkeztünk, uram.\nEz a főszint.",
      rawText:
        "Dialogue: 0,0:00:01.00,0:00:02.00,Top,,0,0,0,,Megérkeztünk, uram.\\NEz a főszint.",
    },
  ];
}

describe("isCombinedAssSubtitle", () => {
  it("is true only for combined languages in ass/ssa", () => {
    expect(isCombinedAssSubtitle("en:combined-hu", "ass")).toBe(true);
    expect(isCombinedAssSubtitle("en:combined-hu-de", "ssa")).toBe(true);
    expect(isCombinedAssSubtitle("en:combined-hu", "srt")).toBe(false);
    expect(isCombinedAssSubtitle("en", "ass")).toBe(false);
    expect(isCombinedAssSubtitle(undefined, "ass")).toBe(false);
  });
});

describe("mergeCombinedAssCues", () => {
  it("merges same-timestamp dialogues into one stacked cue", () => {
    const merged = mergeCombinedAssCues(combinedAssCues());
    expect(merged).toHaveLength(1);
    expect(merged[0].startMs).toBe(1000);
    expect(merged[0].endMs).toBe(2000);
    expect(merged[0].text).toBe(
      "Here you are, sir.\nMain level, please.\nMegérkeztünk, uram.\nEz a főszint.",
    );
    // Remembers each dialogue's line count and original line.
    const stack = (merged[0].formatMetadata as { combinedStack: unknown[] })
      .combinedStack as Array<{ lineCount: number; rawText: string }>;
    expect(stack.map((m) => m.lineCount)).toEqual([2, 2]);
    expect(stack[0].rawText).toContain("Bottom");
    expect(stack[1].rawText).toContain("Top");
  });

  it("leaves a lone dialogue untouched", () => {
    const single: Cue[] = [
      { id: "x", startMs: 5, endMs: 9, text: "Solo", rawText: "Dialogue: ..." },
    ];
    expect(mergeCombinedAssCues(single)).toEqual(single);
  });
});

describe("splitCombinedAssCues round-trip", () => {
  it("restores the original positioned dialogues", () => {
    const merged = mergeCombinedAssCues(combinedAssCues());
    const split = splitCombinedAssCues(merged);
    expect(split).toHaveLength(2);
    expect(split[0].text).toBe("Here you are, sir.\nMain level, please.");
    expect(split[0].rawText).toContain("Bottom");
    expect(split[1].text).toBe("Megérkeztünk, uram.\nEz a főszint.");
    expect(split[1].rawText).toContain("Top");
    // Timestamps preserved on every split cue.
    expect(split.every((c) => c.startMs === 1000 && c.endMs === 2000)).toBe(
      true,
    );
  });

  it("keeps an in-place text edit on the right language", () => {
    const merged = mergeCombinedAssCues(combinedAssCues());
    // Edit the secondary's second line.
    merged[0].text =
      "Here you are, sir.\nMain level, please.\nMegérkeztünk, uram.\nEz az emelet.";
    const split = splitCombinedAssCues(merged);
    expect(split[0].text).toBe("Here you are, sir.\nMain level, please.");
    expect(split[1].text).toBe("Megérkeztünk, uram.\nEz az emelet.");
  });

  it("drops a language whose lines were all deleted", () => {
    const merged = mergeCombinedAssCues(combinedAssCues());
    // Remove the secondary entirely (leave only the 2 primary lines).
    merged[0].text = "Here you are, sir.\nMain level, please.";
    const split = splitCombinedAssCues(merged);
    expect(split).toHaveLength(1);
    expect(split[0].rawText).toContain("Bottom");
  });

  it("passes through cues without stack metadata", () => {
    const plain: Cue[] = [
      { id: "p", startMs: 0, endMs: 1, text: "x", rawText: "Dialogue: x" },
    ];
    expect(splitCombinedAssCues(plain)).toEqual(plain);
  });
});
