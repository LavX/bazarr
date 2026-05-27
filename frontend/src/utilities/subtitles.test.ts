import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  getSubtitleSyncStatusPresentation,
  getSyncEngineLabel,
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
    expect(isSyncOutputLanguageKey("hu:sync-autosubsync")).toBe(true);
    expect(isSyncOutputLanguageKey("hu:sync-alass")).toBe(true);
    expect(isSyncOutputLanguageKey("hu")).toBe(false);
  });

  it("labels sync engines for the compare workflow", () => {
    expect(getSyncEngineLabel("sync-ffsubsync")).toBe("FFsubsync");
    expect(getSyncEngineLabel("sync-autosubsync")).toBe("Autosubsync");
    expect(getSyncEngineLabel("sync-alass")).toBe("ALASS");
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
