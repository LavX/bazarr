import { beforeEach,describe, expect, it, vi } from "vitest";
import { getParser } from "@/pages/SubtitleEditor/parsers";

// Mock crypto.randomUUID since it may not be available in test env
beforeEach(() => {
  let counter = 0;
  vi.stubGlobal("crypto", {
    randomUUID: () => `test-uuid-${++counter}`,
  });
});

// ---------------------------------------------------------------------------
// Tier 1: SRT
// ---------------------------------------------------------------------------
describe("SRT parser", () => {
  const parser = getParser("srt");

  it("parses a basic SRT file with 2 cues", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:02,500",
      "Hello world",
      "",
      "2",
      "00:01:00,000 --> 00:01:03,400",
      "Second cue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);

    expect(result.cues[0].startMs).toBe(1000);
    expect(result.cues[0].endMs).toBe(2500);
    expect(result.cues[0].text).toBe("Hello world");

    expect(result.cues[1].startMs).toBe(60000);
    expect(result.cues[1].endMs).toBe(63400);
    expect(result.cues[1].text).toBe("Second cue");

    expect(result.metadata.format).toBe("srt");
  });

  it("returns 0 cues for empty input", () => {
    const result = parser.parse("");
    expect(result.cues).toHaveLength(0);
  });

  it("handles multiline cue text", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:04,000",
      "Line one",
      "Line two",
      "Line three",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("Line one\nLine two\nLine three");
  });

  it("handles Windows line endings (\\r\\n)", () => {
    const input =
      "1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n\r\n2\r\n00:00:03,000 --> 00:00:04,000\r\nWorld";

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[1].text).toBe("World");
  });

  it("handles BOM prefix", () => {
    const input =
      "\uFEFF1\n00:00:01,000 --> 00:00:02,000\nWith BOM";

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("With BOM");
  });
});

// ---------------------------------------------------------------------------
// Tier 1: ASS/SSA
// ---------------------------------------------------------------------------
describe("ASS parser", () => {
  const parser = getParser("ass");

  const minimalASS = [
    "[Script Info]",
    "Title: Test Script",
    "ScriptType: v4.00+",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize",
    "Style: Default,Arial,20",
    "",
    "[Events]",
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,Hello world",
    "Dialogue: 0,0:01:00.50,0:01:03.00,Default,,0000,0000,0000,,{\\b1}Bold text{\\b0}\\NSecond line",
  ].join("\n");

  it("parses a minimal ASS file with Script Info, Styles, and Dialogue", () => {
    const result = parser.parse(minimalASS);
    expect(result.metadata.format).toBe("ass");
    expect(result.metadata.title).toBe("Test Script");
    expect(result.cues).toHaveLength(2);

    expect(result.cues[0].startMs).toBe(1000);
    expect(result.cues[0].endMs).toBe(4000);
    expect(result.cues[0].text).toBe("Hello world");
  });

  it("preserves the full original Dialogue line in rawText", () => {
    const result = parser.parse(minimalASS);
    expect(result.cues[1].rawText).toBe(
      "Dialogue: 0,0:01:00.50,0:01:03.00,Default,,0000,0000,0000,,{\\b1}Bold text{\\b0}\\NSecond line",
    );
  });

  it("strips override tags and converts \\N to newlines in text", () => {
    const result = parser.parse(minimalASS);
    expect(result.cues[1].text).toBe("Bold text\nSecond line");
  });

  it("includes header sections in metadata.rawHeader", () => {
    const result = parser.parse(minimalASS);
    const rawHeader = result.metadata.rawHeader as string;
    expect(rawHeader).toContain("[Script Info]");
    expect(rawHeader).toContain("[V4+ Styles]");
    expect(rawHeader).not.toContain("[Events]");
  });
});

// ---------------------------------------------------------------------------
// Tier 1: VTT
// ---------------------------------------------------------------------------
describe("VTT parser", () => {
  const parser = getParser("vtt");

  it("parses with WEBVTT header", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "Hello world",
      "",
      "00:01:00.000 --> 00:01:03.500",
      "Second cue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.format).toBe("vtt");
    expect(result.cues).toHaveLength(2);

    expect(result.cues[0].startMs).toBe(1000);
    expect(result.cues[0].endMs).toBe(4000);
    expect(result.cues[0].text).toBe("Hello world");

    expect(result.cues[1].startMs).toBe(60000);
    expect(result.cues[1].endMs).toBe(63500);
    expect(result.cues[1].text).toBe("Second cue");
  });

  it("handles optional cue identifiers", () => {
    const input = [
      "WEBVTT",
      "",
      "intro",
      "00:00:01.000 --> 00:00:04.000",
      "With identifier",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("With identifier");
  });

  it("handles position/alignment settings in timestamp line", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000 position:10% align:start",
      "Positioned cue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].startMs).toBe(1000);
    expect(result.cues[0].endMs).toBe(4000);
    expect(result.cues[0].text).toBe("Positioned cue");
  });
});

// ---------------------------------------------------------------------------
// Tier 2: SUB (MicroDVD)
// ---------------------------------------------------------------------------
describe("SUB parser", () => {
  it("parses frame-based lines and converts to ms at 23.976fps", () => {
    const parser = getParser("sub");
    const input = "{0}{100}Hello";

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.metadata.format).toBe("sub");

    // 0 frames = 0ms, 100 frames at 23.976fps = Math.round((100/23.976)*1000)
    expect(result.cues[0].startMs).toBe(0);
    expect(result.cues[0].endMs).toBe(Math.round((100 / 23.976) * 1000));
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[0].rawText).toBe("{0}{100}Hello");
  });
});

// ---------------------------------------------------------------------------
// Tier 2: SMI
// ---------------------------------------------------------------------------
describe("SMI parser", () => {
  it("parses SYNC-based content", () => {
    const parser = getParser("smi");
    const input = [
      "<SAMI>",
      "<BODY>",
      '<SYNC Start=1000>Hello',
      '<SYNC Start=4000>&nbsp;',
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.format).toBe("smi");
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].startMs).toBe(1000);
    expect(result.cues[0].endMs).toBe(4000);
    expect(result.cues[0].text).toBe("Hello");
  });
});

// ---------------------------------------------------------------------------
// Tier 2: TXT (TMPlayer)
// ---------------------------------------------------------------------------
describe("TXT parser", () => {
  it("parses TMPlayer format", () => {
    const parser = getParser("txt");
    const input = ["00:01:00:Hello", "00:02:00:World"].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.format).toBe("txt");
    expect(result.cues).toHaveLength(2);

    expect(result.cues[0].startMs).toBe(60000);
    // End time adjusted to next cue's start
    expect(result.cues[0].endMs).toBe(120000);
    expect(result.cues[0].text).toBe("Hello");

    expect(result.cues[1].startMs).toBe(120000);
    // Last cue gets startMs + 3000
    expect(result.cues[1].endMs).toBe(123000);
    expect(result.cues[1].text).toBe("World");
  });
});

// ---------------------------------------------------------------------------
// Tier 2: MPL
// ---------------------------------------------------------------------------
describe("MPL parser", () => {
  it("parses decisecond-based lines and converts to ms", () => {
    const parser = getParser("mpl");
    const input = "[100][200]Hello";

    const result = parser.parse(input);
    expect(result.metadata.format).toBe("mpl");
    expect(result.cues).toHaveLength(1);

    // 100 deciseconds = 10000ms, 200 deciseconds = 20000ms
    expect(result.cues[0].startMs).toBe(10000);
    expect(result.cues[0].endMs).toBe(20000);
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[0].rawText).toBe("[100][200]Hello");
  });
});
