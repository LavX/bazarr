/* eslint-disable camelcase */

import {
  episodeCode,
  episodeCodeOf,
  wantedLanguagesForAudio,
} from "./episodes";

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

describe("wantedLanguagesForAudio", () => {
  const items = [
    { language: "fr" },
    { language: "en", audio_exclude: "True" },
    { language: "de", audio_only_include: "True" },
    { language: "es", audio_exclude: "False", audio_only_include: "False" },
  ];

  it("drops a language the audio rules exclude for this audio", () => {
    expect(wantedLanguagesForAudio(items, [{ code2: "en" }])).toEqual([
      "fr",
      "es",
    ]);
  });

  it("keeps an exclude-rule language when the audio is another one", () => {
    expect(wantedLanguagesForAudio(items, [{ code2: "ja" }])).toEqual([
      "fr",
      "en",
      "es",
    ]);
  });

  it("keeps an only-include language only for that audio", () => {
    expect(wantedLanguagesForAudio(items, [{ code2: "de" }])).toEqual([
      "fr",
      "en",
      "de",
      "es",
    ]);
  });

  it("treats items without the audio keys as unrestricted", () => {
    expect(wantedLanguagesForAudio([{ language: "fr" }], [])).toEqual(["fr"]);
  });
});
