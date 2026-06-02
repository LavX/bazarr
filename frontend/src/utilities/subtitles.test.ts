import {
  buildComparableSubtitleVariantKey,
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  getCombinedLabel,
  getCombinedSecondaries,
  getSubtitleSyncStatusPresentation,
  getSyncEngineLabel,
  isCombinedOutputSubtitle,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputLanguageKey,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";

describe("subtitle language helpers", () => {
  it("keeps sync modifiers in language keys", () => {
    const subtitle: Subtitle = {
      code2: "hu",
      name: "Hungarian",
      forced: false,
      hi: false,
      modifier: "sync-ffsubsync",
      language: "hu:sync-ffsubsync",
      path: "/movie/Movie.hu.ffsubsync.srt",
    };

    expect(buildSubtitleLanguageKey(subtitle)).toBe("hu:sync-ffsubsync");
  });

  it("falls back to legacy hi and forced flags", () => {
    expect(
      buildSubtitleLanguageKey({
        code2: "en",
        name: "English",
        forced: false,
        hi: true,
        path: "/movie/Movie.en.hi.srt",
      }),
    ).toBe("en:hi");
  });

  it("detects generated sync output subtitles", () => {
    expect(
      isSyncOutputSubtitle({
        code2: "hu",
        name: "Hungarian",
        forced: false,
        hi: false,
        modifier: "sync-alass",
        path: "/movie/Movie.hu.alass.srt",
      }),
    ).toBe(true);
  });

  it("detects generated sync output subtitles by filename", () => {
    expect(
      isSyncOutputSubtitle({
        code2: "hu",
        name: "Hungarian",
        forced: false,
        hi: false,
        path: "/movie/Movie.hu.autosubsync.srt",
      }),
    ).toBe(true);
  });

  it("does not detect ordinary subtitles with engine tokens in the title", () => {
    expect(
      isSyncOutputSubtitle({
        code2: "hu",
        name: "Hungarian",
        forced: false,
        hi: false,
        path: "/movie/Movie.ffsubsync.Release.hu.srt",
      }),
    ).toBe(false);
  });

  it("does not allow generated sync outputs to be synchronized again", () => {
    expect(
      canSynchronizeSubtitle({
        code2: "hu",
        name: "Hungarian",
        forced: false,
        hi: false,
        modifier: "sync-alass",
        path: "/movie/Movie.hu.alass.srt",
      }),
    ).toBe(false);
  });

  it("detects generated sync output language keys", () => {
    expect(isSyncOutputLanguageKey("hu:sync-ffsubsync")).toBe(true);
    expect(isSyncOutputLanguageKey("hu:hi:sync-ffsubsync")).toBe(true);
    expect(isSyncOutputLanguageKey("hu:sync-autosubsync")).toBe(true);
    expect(isSyncOutputLanguageKey("hu:sync-alass")).toBe(true);
    expect(isSyncOutputLanguageKey("hu")).toBe(false);
  });

  it("labels sync engines for the compare workflow", () => {
    expect(getSyncEngineLabel("sync-ffsubsync")).toBe("FFsubsync");
    expect(getSyncEngineLabel("sync-autosubsync")).toBe("Autosubsync");
    expect(getSyncEngineLabel("sync-alass")).toBe("ALASS");
  });

  it("normalizes comparable variants without sync modifiers", () => {
    expect(
      buildComparableSubtitleVariantKey({
        code2: "en",
        name: "English",
        forced: false,
        hi: true,
        modifier: "sync-ffsubsync",
        language: "en:hi:sync-ffsubsync",
        path: "/movie/Movie.en.hi.ffsubsync.srt",
      }),
    ).toBe("en:hi");
  });

  it("matches sync outputs only for the same subtitle variant", () => {
    const regular: Subtitle = {
      code2: "en",
      name: "English",
      forced: false,
      hi: false,
      language: "en",
      path: "/movie/Movie.en.srt",
    };
    const regularOutput: Subtitle = {
      code2: "en",
      name: "English",
      forced: false,
      hi: false,
      modifier: "sync-ffsubsync",
      language: "en:sync-ffsubsync",
      path: "/movie/Movie.en.ffsubsync.srt",
    };
    const hiOutput: Subtitle = {
      code2: "en",
      name: "English",
      forced: false,
      hi: true,
      modifier: "sync-ffsubsync",
      language: "en:hi:sync-ffsubsync",
      path: "/movie/Movie.en.hi.ffsubsync.srt",
    };

    expect(isCompatibleSyncOutputSubtitle(regular, regularOutput)).toBe(true);
    expect(isCompatibleSyncOutputSubtitle(regular, hiOutput)).toBe(false);
  });

  it("shows an unconfirmed sync state for edited subtitles", () => {
    expect(
      getSubtitleSyncStatusPresentation({
        synced: true,
        confirmed: false,
        editedAfterSync: true,
        lastModified: 1,
        lastSyncTimestamp: "2026-05-27T12:00:00",
      }),
    ).toEqual({
      icon: "question",
      label: "Sync unconfirmed",
    });
  });

  it("keeps the sync status when backend confirms it", () => {
    expect(
      getSubtitleSyncStatusPresentation({
        synced: true,
        confirmed: true,
        editedAfterSync: false,
        lastModified: 1,
        lastSyncTimestamp: "2026-05-27T12:00:00",
        jobStatus: null,
        jobId: null,
      }),
    ).toEqual({
      icon: "sync",
      label: "Sync",
    });
  });

  it("shows active sync state before confirmed history", () => {
    expect(
      getSubtitleSyncStatusPresentation({
        synced: true,
        confirmed: true,
        editedAfterSync: false,
        lastModified: 1,
        lastSyncTimestamp: "2026-05-27T12:00:00",
        jobStatus: "running",
        jobId: 42,
      }),
    ).toEqual({
      icon: "running",
      label: "Sync running",
    });
  });

  it("does not show a sync status for never-synced subtitles", () => {
    expect(
      getSubtitleSyncStatusPresentation({
        synced: false,
        confirmed: false,
        editedAfterSync: false,
        lastModified: 1,
        lastSyncTimestamp: null,
        jobStatus: null,
        jobId: null,
      }),
    ).toBeNull();
  });
});

describe("combined output helpers", () => {
  it("detects combined output via modifier", () => {
    expect(
      isCombinedOutputSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "combined-hu",
        language: "en:combined-hu",
        path: "/movie/Movie.en.combined-hu.srt",
      }),
    ).toBe(true);
  });

  it("detects combined output via language key when modifier is missing", () => {
    expect(
      isCombinedOutputSubtitle({
        code2: "de",
        name: "German",
        forced: false,
        hi: false,
        language: "de:combined-es-zh",
        path: "/movie/Movie.de.combined-es-zh.ass",
      }),
    ).toBe(true);
  });

  it("does not flag regular subtitles or sync outputs as combined", () => {
    expect(
      isCombinedOutputSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        language: "en",
        path: "/movie/Movie.en.srt",
      }),
    ).toBe(false);

    expect(
      isCombinedOutputSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "sync-ffsubsync",
        language: "en:sync-ffsubsync",
        path: "/movie/Movie.en.ffsubsync.srt",
      }),
    ).toBe(false);
  });

  it("extracts secondary codes from a combined output", () => {
    expect(
      getCombinedSecondaries({
        code2: "de",
        name: "German",
        forced: false,
        hi: false,
        modifier: "combined-es-zh",
        language: "de:combined-es-zh",
        path: "/movie/Movie.de.combined-es-zh.ass",
      }),
    ).toEqual(["es", "zh"]);
  });

  it("returns null secondaries for non-combined subtitles", () => {
    expect(
      getCombinedSecondaries({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        language: "en",
        path: "/movie/Movie.en.srt",
      }),
    ).toBeNull();
  });

  it("formats combined label as PRIMARY + SEC1 [+ SEC2]", () => {
    expect(
      getCombinedLabel({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "combined-hu",
        language: "en:combined-hu",
        path: "/movie/Movie.en.combined-hu.srt",
      }),
    ).toBe("EN + HU");

    expect(
      getCombinedLabel({
        code2: "de",
        name: "German",
        forced: false,
        hi: false,
        modifier: "combined-es-zh",
        language: "de:combined-es-zh",
        path: "/movie/Movie.de.combined-es-zh.ass",
      }),
    ).toBe("DE + ES + ZH");
  });

  it("allows sync action on combined output subtitles (they are valid SRTs)", () => {
    expect(
      canSynchronizeSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "combined-hu",
        language: "en:combined-hu",
        path: "/movie/Movie.en.combined-hu.srt",
      }),
    ).toBe(true);
  });

  it("blocks sync action on sync-engine output subtitles", () => {
    expect(
      canSynchronizeSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "sync-ffsubsync",
        language: "en:sync-ffsubsync",
        path: "/movie/Movie.en.ffsubsync.srt",
      }),
    ).toBe(false);
  });

  it("still allows sync action on plain subtitles", () => {
    expect(
      canSynchronizeSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        language: "en",
        path: "/movie/Movie.en.srt",
      }),
    ).toBe(true);
  });
});

describe("combineRequestForSubtitle", () => {
  it("reproduces the artifact's languages and SRT format", () => {
    expect(
      combineRequestForSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        modifier: "combined-hu",
        language: "en:combined-hu",
        path: "/movie/Movie.en.combined-hu.srt",
      }),
    ).toEqual({ languages: ["en", "hu"], format: "srt" });
  });

  it("reproduces three languages and ASS format", () => {
    expect(
      combineRequestForSubtitle({
        code2: "de",
        name: "German",
        forced: false,
        hi: false,
        modifier: "combined-es-zh",
        language: "de:combined-es-zh",
        path: "/movie/Movie.de.combined-es-zh.ass",
      }),
    ).toEqual({ languages: ["de", "es", "zh"], format: "ass" });
  });

  it("returns null for non-combined subtitles", () => {
    expect(
      combineRequestForSubtitle({
        code2: "en",
        name: "English",
        forced: false,
        hi: false,
        language: "en",
        path: "/movie/Movie.en.srt",
      }),
    ).toBeNull();
  });
});
