import { episodeCode, episodeCodeOf } from "./episodes";

describe("episodeCode", () => {
  it("pads the season and episode to two digits", () => {
    expect(episodeCode(1, 1)).toBe("S01E01");
    expect(episodeCode(0, 3)).toBe("S00E03");
  });

  it("keeps numbers longer than two digits whole", () => {
    expect(episodeCode(12, 105)).toBe("S12E105");
  });
});

describe("episodeCodeOf", () => {
  it("reads the season x episode form the history API sends", () => {
    expect(episodeCodeOf("1x1")).toBe("S01E01");
    expect(episodeCodeOf("10x12")).toBe("S10E12");
  });

  it("returns anything else as it came", () => {
    expect(episodeCodeOf("S05E07")).toBe("S05E07");
    expect(episodeCodeOf("")).toBe("");
  });
});
