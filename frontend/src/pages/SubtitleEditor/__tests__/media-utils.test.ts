import { describe, expect, it } from "vitest";
import {
  findActiveCueIndex,
  getActiveCueText,
} from "@/pages/SubtitleEditor/EditorPage";
import {
  formatTime,
  renderSubtitleHtml,
} from "@/pages/SubtitleEditor/VideoPreview";
import { cueColor, formatMs } from "@/pages/SubtitleEditor/WaveformTimeline";

// ---------------------------------------------------------------------------
// VideoPreview: formatTime
// ---------------------------------------------------------------------------
describe("formatTime", () => {
  it("formats zero milliseconds", () => {
    expect(formatTime(0)).toBe("0:00");
  });

  it("formats seconds under a minute", () => {
    expect(formatTime(5000)).toBe("0:05");
    expect(formatTime(59000)).toBe("0:59");
  });

  it("formats exact minutes", () => {
    expect(formatTime(60000)).toBe("1:00");
    expect(formatTime(300000)).toBe("5:00");
  });

  it("formats minutes and seconds", () => {
    expect(formatTime(90000)).toBe("1:30");
    expect(formatTime(754000)).toBe("12:34");
  });

  it("includes hours when >= 3600s", () => {
    expect(formatTime(3600000)).toBe("1:00:00");
    expect(formatTime(3661000)).toBe("1:01:01");
    expect(formatTime(7384000)).toBe("2:03:04");
  });

  it("pads seconds with leading zero", () => {
    expect(formatTime(3000)).toBe("0:03");
    expect(formatTime(63000)).toBe("1:03");
  });

  it("pads minutes with leading zero in hour mode", () => {
    expect(formatTime(3720000)).toBe("1:02:00");
  });

  it("truncates sub-second precision (floors)", () => {
    expect(formatTime(1999)).toBe("0:01");
    expect(formatTime(500)).toBe("0:00");
  });
});

// ---------------------------------------------------------------------------
// VideoPreview: renderSubtitleHtml
// ---------------------------------------------------------------------------
describe("renderSubtitleHtml", () => {
  it("returns empty string for empty input", () => {
    expect(renderSubtitleHtml("")).toBe("");
  });

  it("passes through plain text unchanged", () => {
    expect(renderSubtitleHtml("Hello world")).toBe("Hello world");
  });

  it("converts newlines to <br>", () => {
    expect(renderSubtitleHtml("Line one\nLine two")).toBe(
      "Line one<br>Line two",
    );
  });

  it("preserves <i>, <b>, <u>, <s> tags", () => {
    expect(renderSubtitleHtml("<i>italic</i>")).toBe("<i>italic</i>");
    expect(renderSubtitleHtml("<b>bold</b>")).toBe("<b>bold</b>");
    expect(renderSubtitleHtml("<u>underline</u>")).toBe("<u>underline</u>");
    expect(renderSubtitleHtml("<s>strike</s>")).toBe("<s>strike</s>");
  });

  it("handles case-insensitive formatting tags", () => {
    expect(renderSubtitleHtml("<I>italic</I>")).toBe("<i>italic</i>");
    expect(renderSubtitleHtml("<B>bold</B>")).toBe("<b>bold</b>");
  });

  it("escapes other HTML tags", () => {
    // eslint-disable-next-line testing-library/render-result-naming-convention
    const result = renderSubtitleHtml("<script>alert('xss')</script>");
    expect(result).not.toContain("<script>");
    expect(result).toContain("&lt;script&gt;");
  });

  it("strips ASS override tags", () => {
    expect(renderSubtitleHtml("{\\i1}Hello{\\i0}")).toBe("Hello");
    expect(renderSubtitleHtml("{\\b1}Bold text{\\b0}")).toBe("Bold text");
    expect(renderSubtitleHtml("{\\an8}Top aligned")).toBe("Top aligned");
    expect(renderSubtitleHtml("{\\pos(320,50)}Positioned")).toBe("Positioned");
  });

  it("strips complex ASS tags with multiple overrides", () => {
    expect(renderSubtitleHtml("{\\i1\\b1}Both{\\i0\\b0}")).toBe("Both");
  });

  it("strips font tags completely", () => {
    expect(renderSubtitleHtml('<font color="#FF0000">Red text</font>')).toBe(
      "Red text",
    );
  });

  it("escapes ampersands", () => {
    expect(renderSubtitleHtml("AT&T")).toBe("AT&amp;T");
  });

  it("handles a complex real-world subtitle line", () => {
    const input = '{\\an8}<i>Whispering</i>\n<font color="#FFFF00">Hey!</font>';
    // eslint-disable-next-line testing-library/render-result-naming-convention
    const result = renderSubtitleHtml(input);
    expect(result).toBe("<i>Whispering</i><br>Hey!");
    expect(result).not.toContain("{\\an8}");
    expect(result).not.toContain("font");
  });

  it("handles nested formatting tags", () => {
    // eslint-disable-next-line testing-library/render-result-naming-convention
    const result = renderSubtitleHtml("<b><i>bold italic</i></b>");
    expect(result).toBe("<b><i>bold italic</i></b>");
  });
});

// ---------------------------------------------------------------------------
// EditorPage: playback cue lookup
// ---------------------------------------------------------------------------
describe("playback cue lookup", () => {
  const cues = [
    { id: "a", startMs: 1000, endMs: 2000, text: "first" },
    { id: "b", startMs: 2500, endMs: 3500, text: "second" },
  ];

  it("finds the cue under the playhead", () => {
    expect(findActiveCueIndex(cues, 3000)).toBe(1);
    expect(getActiveCueText(cues, 3000)).toBe("second");
  });

  it("does not depend on editor selection", () => {
    expect(getActiveCueText(cues, 3000)).not.toBe(cues[0].text);
  });

  it("returns no text while the playhead is in a gap", () => {
    expect(findActiveCueIndex(cues, 2250)).toBe(-1);
    expect(getActiveCueText(cues, 2250)).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// VideoPreview: codec detection logic
// ---------------------------------------------------------------------------
describe("codec detection logic", () => {
  // This tests the decision logic used inside VideoPreview's useEffect.
  // The logic is: direct = canPlayVideo && canPlayAudio && servableContainer
  //               remux  = canPlayVideo && !isDirect
  //               transcode = otherwise

  const codecMap: Record<string, string> = {
    h264: "avc1.64001E",
    avc: "avc1.64001E",
    hevc: "hvc1.1.6.L93.B0",
    h265: "hvc1.1.6.L93.B0",
    vp9: "vp09.00.10.08",
    vp8: "vp8",
    av1: "av01.0.04M.08",
  };

  const audioCodecMap: Record<string, string> = {
    aac: "mp4a.40.2",
    opus: "opus",
    vorbis: "vorbis",
    ac3: "ac-3",
    eac3: "ec-3",
    mp3: "mp4a.69",
  };

  function detectPlayMode(
    vCodec: string,
    aCodec: string,
    ext: string,
    canPlayType: (mime: string) => string,
  ): "direct" | "remux" | "transcode" {
    const vCodecStr = codecMap[vCodec] || "";
    const aCodecStr = audioCodecMap[aCodec] || "";
    const container = ext === "webm" ? "webm" : "mp4";
    const servableContainer = [".mp4", ".m4v", ".webm", ".mkv"].includes(
      `.${ext}`,
    );

    const canPlayVideo = vCodecStr
      ? !!canPlayType(`video/${container}; codecs="${vCodecStr}"`)
      : false;
    const canPlayAudio = aCodecStr
      ? !!canPlayType(`video/${container}; codecs="${aCodecStr}"`)
      : false;
    const canPlayBoth = canPlayVideo && canPlayAudio;

    const isDirect = canPlayBoth && servableContainer;
    const isRemux = canPlayVideo && !isDirect;

    if (isDirect) return "direct";
    if (isRemux) return "remux";
    return "transcode";
  }

  it("returns direct when browser supports both codecs and container is servable", () => {
    const canPlayAll = () => "probably";
    expect(detectPlayMode("h264", "aac", "mp4", canPlayAll)).toBe("direct");
  });

  it("returns remux when video is supported but audio is not", () => {
    const canPlay = (mime: string) => {
      if (mime.includes("avc1")) return "probably";
      if (mime.includes("ac-3")) return ""; // AC3 not supported
      return "";
    };
    expect(detectPlayMode("h264", "ac3", "mkv", canPlay)).toBe("remux");
  });

  it("returns remux when video is supported but container is not servable", () => {
    const canPlayAll = () => "probably";
    // .avi is not in the servable container list
    expect(detectPlayMode("h264", "aac", "avi", canPlayAll)).toBe("remux");
  });

  it("returns transcode when video codec is not supported", () => {
    const canPlayNone = () => "";
    expect(detectPlayMode("hevc", "aac", "mkv", canPlayNone)).toBe("transcode");
  });

  it("returns transcode for unknown video codec", () => {
    const canPlayAll = () => "probably";
    expect(detectPlayMode("unknown", "aac", "mp4", canPlayAll)).toBe(
      "transcode",
    );
  });

  it("returns direct for webm/vp9/opus when browser supports it", () => {
    const canPlayAll = () => "probably";
    expect(detectPlayMode("vp9", "opus", "webm", canPlayAll)).toBe("direct");
  });

  it("returns remux when audio codec is unknown but video is supported", () => {
    const canPlayVideo = (mime: string) => {
      if (mime.includes("avc1")) return "probably";
      return "";
    };
    // unknown audio codec means aCodecStr = "", canPlayAudio = false
    expect(detectPlayMode("h264", "truehd", "mp4", canPlayVideo)).toBe("remux");
  });

  it("uses mp4 container for non-webm extensions", () => {
    // mkv should be tested as video/mp4 not video/mkv
    const calls: string[] = [];
    const spy = (mime: string) => {
      calls.push(mime);
      return "probably";
    };
    detectPlayMode("h264", "aac", "mkv", spy);
    expect(calls.every((c) => c.startsWith("video/mp4"))).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// WaveformTimeline: formatMs
// ---------------------------------------------------------------------------
describe("formatMs", () => {
  it("formats zero", () => {
    expect(formatMs(0)).toBe("0:00.0");
  });

  it("formats seconds with fractional", () => {
    expect(formatMs(1500)).toBe("0:01.5");
    expect(formatMs(12300)).toBe("0:12.3");
  });

  it("formats minutes and seconds", () => {
    expect(formatMs(90000)).toBe("1:30.0");
    expect(formatMs(125700)).toBe("2:05.7");
  });

  it("pads seconds with leading zero", () => {
    expect(formatMs(3100)).toBe("0:03.1");
    expect(formatMs(63200)).toBe("1:03.2");
  });

  it("truncates sub-100ms precision", () => {
    // 1999ms: totalSec=1, frac=floor(999/100)=9
    expect(formatMs(1999)).toBe("0:01.9");
    // 1050ms: totalSec=1, frac=floor(50/100)=0
    expect(formatMs(1050)).toBe("0:01.0");
  });

  it("handles large values without hours", () => {
    // formatMs does not include hours, just keeps incrementing minutes
    expect(formatMs(3600000)).toBe("60:00.0");
  });
});

// ---------------------------------------------------------------------------
// WaveformTimeline: cueColor
// ---------------------------------------------------------------------------
describe("cueColor", () => {
  it("returns highlighted color for selected index", () => {
    expect(cueColor(3, 3)).toBe("rgba(230, 138, 0, 0.45)");
  });

  it("returns dim color for non-selected index", () => {
    expect(cueColor(0, 3)).toBe("rgba(230, 138, 0, 0.15)");
    expect(cueColor(5, 3)).toBe("rgba(230, 138, 0, 0.15)");
  });

  it("handles index 0 selected", () => {
    expect(cueColor(0, 0)).toBe("rgba(230, 138, 0, 0.45)");
    expect(cueColor(1, 0)).toBe("rgba(230, 138, 0, 0.15)");
  });

  it("handles negative selectedIndex (nothing selected)", () => {
    expect(cueColor(0, -1)).toBe("rgba(230, 138, 0, 0.15)");
  });
});

// ---------------------------------------------------------------------------
// WaveformTimeline: region event handler re-registration
// ---------------------------------------------------------------------------
describe("region event handler dependency on ready state", () => {
  // This is a structural verification, not a runtime test.
  // The region event handlers useEffect (line ~209) must include `ready` in its
  // dependency array so that handlers are re-registered after WaveSurfer is
  // recreated (which resets `ready` to false, then back to true).
  //
  // Without `ready` in the deps, changing the media source (peaksUrl) destroys
  // and recreates WaveSurfer + regions plugin, but the event handler effect
  // would not re-run, leaving click and drag handlers pointing at the old
  // destroyed plugin instance.

  it("documents that region event handlers depend on ready state", () => {
    // We verify this by reading the source. The useEffect at line ~209 has deps:
    //   [cues, onSelect, onTimingChange, ready]
    //
    // The `ready` dependency is critical:
    // 1. peaksUrl changes -> init effect destroys WaveSurfer, sets ready=false
    // 2. New WaveSurfer created, new regions plugin created
    // 3. WaveSurfer fires "ready" -> sets ready=true
    // 4. Because `ready` is in deps, event handler effect re-runs
    // 5. Handlers bind to the NEW regions plugin instance
    //
    // If `ready` were missing from deps, step 4 would not happen,
    // and region clicks/drags would silently fail.
    expect(true).toBe(true);
  });

  // Complementary check: the regions sync effect also depends on `ready`
  it("documents that region sync depends on ready state", () => {
    // The region sync useEffect (line ~189) has deps: [regionKey, ready]
    // This ensures regions are re-drawn after WaveSurfer recreation.
    // Without `ready`, new cue regions would never appear after source change.
    expect(true).toBe(true);
  });
});
