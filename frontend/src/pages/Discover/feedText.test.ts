import { describe, expect, it } from "vitest";
import { readableFeedDate } from "./feedText";

describe("readableFeedDate", () => {
  it("renders a feed day in the reader's own date words", () => {
    const formatted = readableFeedDate("2026-09-09");
    expect(formatted).not.toBe("2026-09-09");
    expect(formatted).toContain("2026");
  });

  it("returns a non-day value untouched instead of inventing a date", () => {
    expect(readableFeedDate("not-a-date")).toBe("not-a-date");
    expect(readableFeedDate("2026-13-45")).toBe("2026-13-45");
    expect(readableFeedDate("2026-02-30")).toBe("2026-02-30");
  });
});
