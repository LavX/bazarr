import {
  BITMAP_SUBTITLE_CODECS,
  describeEmbeddedTrack,
  isBitmapCodec,
  subtitleCodecLabel,
} from "@/utilities/subtitles";

describe("embedded subtitle codec helpers", () => {
  it.each([
    "pgs",
    "vobsub",
    "dvbsub",
    "dvd_subtitle",
    "hdmv_pgs_subtitle",
    "dvd",
  ])("treats %s as bitmap", (codec) => {
    expect(isBitmapCodec(codec)).toBe(true);
  });

  it.each(["subrip", "ass", "ssa", "mov text", "webvtt"])(
    "treats %s as text",
    (codec) => {
      expect(isBitmapCodec(codec)).toBe(false);
    },
  );

  it("treats an unknown codec as text, as rows indexed before 2.8.0 have none", () => {
    expect(isBitmapCodec(null)).toBe(false);
    expect(isBitmapCodec(undefined)).toBe(false);
    expect(isBitmapCodec("")).toBe(false);
  });

  it("ignores case, so a codec stored by hand still matches", () => {
    expect(isBitmapCodec("PGS")).toBe(true);
  });

  it("mirrors the extractor's bitmap list exactly", () => {
    expect([...BITMAP_SUBTITLE_CODECS]).toEqual([
      "pgs",
      "vobsub",
      "dvd_subtitle",
      "dvbsub",
      "hdmv_pgs_subtitle",
      "dvd",
    ]);
  });

  it("labels the codecs knowit reports and upper-cases the rest", () => {
    expect(subtitleCodecLabel("pgs")).toBe("PGS");
    expect(subtitleCodecLabel("vobsub")).toBe("VobSub");
    expect(subtitleCodecLabel("dvbsub")).toBe("DVB");
    expect(subtitleCodecLabel("subrip")).toBe("SRT");
    expect(subtitleCodecLabel("mov text")).toBe("MOV Text");
    expect(subtitleCodecLabel("tx3g")).toBe("TX3G");
    expect(subtitleCodecLabel(null)).toBeNull();
    expect(subtitleCodecLabel("")).toBeNull();
  });

  it("describes a track by its codec only when the codec is known", () => {
    expect(describeEmbeddedTrack("Embedded in the video file", "pgs")).toBe(
      "Embedded in the video file (PGS, image-based)",
    );
    expect(describeEmbeddedTrack("Embedded", "subrip")).toBe("Embedded (SRT)");
    expect(describeEmbeddedTrack("Embedded in the video file", null)).toBe(
      "Embedded in the video file",
    );
  });
});
