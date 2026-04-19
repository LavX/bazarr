import { beforeEach, describe, expect, it, vi } from "vitest";
import { detectFormat, getParser } from "@/pages/SubtitleEditor/parsers";

// Mock crypto.randomUUID since it may not be available in test env
beforeEach(() => {
  let counter = 0;
  vi.stubGlobal("crypto", {
    randomUUID: () => `test-uuid-${++counter}`,
  });
});

// ---------------------------------------------------------------------------
// Format detection
// ---------------------------------------------------------------------------
describe("detectFormat", () => {
  it("detects common extensions", () => {
    expect(detectFormat("movie.srt")).toBe("srt");
    expect(detectFormat("movie.ass")).toBe("ass");
    expect(detectFormat("movie.ssa")).toBe("ssa");
    expect(detectFormat("movie.vtt")).toBe("vtt");
    expect(detectFormat("movie.sub")).toBe("sub");
    expect(detectFormat("movie.smi")).toBe("smi");
    expect(detectFormat("movie.sami")).toBe("smi");
    expect(detectFormat("movie.txt")).toBe("txt");
    expect(detectFormat("movie.mpl")).toBe("mpl");
  });

  it("is case-insensitive for extensions", () => {
    expect(detectFormat("movie.SRT")).toBe("srt");
    expect(detectFormat("movie.Ass")).toBe("ass");
    expect(detectFormat("movie.VTT")).toBe("vtt");
  });

  it("defaults to srt for unknown extensions", () => {
    expect(detectFormat("movie.xyz")).toBe("srt");
  });

  it("defaults to srt for filenames without extension", () => {
    expect(detectFormat("movie")).toBe("srt");
  });

  it("handles filenames with multiple dots", () => {
    expect(detectFormat("movie.en.forced.srt")).toBe("srt");
    expect(detectFormat("movie.2024.ass")).toBe("ass");
  });

  it("handles paths with directories", () => {
    expect(detectFormat("/media/movies/movie.vtt")).toBe("vtt");
  });
});

// ---------------------------------------------------------------------------
// getParser
// ---------------------------------------------------------------------------
describe("getParser", () => {
  it("returns a parser for each supported format", () => {
    const formats = [
      "srt",
      "ass",
      "ssa",
      "vtt",
      "sub",
      "smi",
      "txt",
      "mpl",
    ] as const;
    for (const fmt of formats) {
      const parser = getParser(fmt);
      expect(parser).toBeDefined();
      expect(typeof parser.parse).toBe("function");
    }
  });

  it("uses the same parser for ass and ssa", () => {
    expect(getParser("ass")).toBe(getParser("ssa"));
  });
});

// ===========================================================================
// SRT Parser
// ===========================================================================
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
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("   \n\n  ").cues).toHaveLength(0);
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
    const input = "\uFEFF1\n00:00:01,000 --> 00:00:02,000\nWith BOM";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("With BOM");
  });

  it("accepts period as millisecond separator", () => {
    const input = [
      "1",
      "00:00:01.500 --> 00:00:03.750",
      "Period separator",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].startMs).toBe(1500);
    expect(result.cues[0].endMs).toBe(3750);
  });

  it("handles missing sequence numbers (timestamp on first line)", () => {
    const input = ["00:00:01,000 --> 00:00:02,000", "No sequence number"].join(
      "\n",
    );

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("No sequence number");
  });

  it("preserves HTML tags in text", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:04,000",
      "<i>Italic text</i>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("<i>Italic text</i>");
  });

  it("preserves special characters in text", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:04,000",
      'Caf\u00e9 & "quotes" <angle>',
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe('Caf\u00e9 & "quotes" <angle>');
  });

  it("skips malformed blocks without timestamps", () => {
    const input = [
      "1",
      "This is not a timestamp",
      "Some text",
      "",
      "2",
      "00:00:05,000 --> 00:00:06,000",
      "Valid cue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("Valid cue");
  });

  it("handles multiple empty lines between blocks", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:02,000",
      "First",
      "",
      "",
      "",
      "2",
      "00:00:03,000 --> 00:00:04,000",
      "Second",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("handles large hour values", () => {
    const input = ["1", "99:59:59,999 --> 100:00:00,000", "Far future"].join(
      "\n",
    );

    const result = parser.parse(input);
    expect(result.cues[0].startMs).toBe(
      99 * 3600000 + 59 * 60000 + 59 * 1000 + 999,
    );
  });

  it("assigns unique IDs to each cue", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:02,000",
      "First",
      "",
      "2",
      "00:00:03,000 --> 00:00:04,000",
      "Second",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].id).not.toBe(result.cues[1].id);
  });

  it("handles timestamp with only two digit milliseconds", () => {
    const input = ["1", "00:00:01,50 --> 00:00:02,50", "Short ms"].join("\n");

    const result = parser.parse(input);
    // "50" padded to "500"
    expect(result.cues[0].startMs).toBe(1500);
    expect(result.cues[0].endMs).toBe(2500);
  });
});

// ===========================================================================
// VTT Parser
// ===========================================================================
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
  });

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("   \n  ").cues).toHaveLength(0);
  });

  it("returns 0 cues when WEBVTT header is missing", () => {
    const input = ["00:00:01.000 --> 00:00:04.000", "No header"].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(0);
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

  it("handles MM:SS.mmm timestamps without hours", () => {
    const input = [
      "WEBVTT",
      "",
      "01:30.500 --> 02:00.000",
      "Short timestamp",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].startMs).toBe(90500);
    expect(result.cues[0].endMs).toBe(120000);
  });

  it("handles multiline cue text", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "Line one",
      "Line two",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Line one\nLine two");
  });

  it("preserves HTML/VTT tags in text", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "<v Speaker>Hello <i>world</i></v>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("<v Speaker>Hello <i>world</i></v>");
  });

  it("extracts STYLE blocks from header", () => {
    const input = [
      "WEBVTT",
      "",
      "STYLE",
      "::cue { color: white; }",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "Styled cue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.metadata.styles).toBe("::cue { color: white; }");
  });

  it("preserves rawHeader metadata", () => {
    const input = [
      "WEBVTT - My Title",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "Hello",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.rawHeader).toContain("WEBVTT - My Title");
  });

  it("handles BOM prefix", () => {
    const input = "\uFEFFWEBVTT\n\n00:00:01.000 --> 00:00:02.000\nBOM cue";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("BOM cue");
  });

  it("handles Windows line endings", () => {
    const input =
      "WEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nHello\r\n\r\n00:00:03.000 --> 00:00:04.000\r\nWorld";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("handles special characters", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "\u00e9\u00e0\u00fc \u2014 \u266a \u2190",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("\u00e9\u00e0\u00fc \u2014 \u266a \u2190");
  });
});

// ===========================================================================
// ASS/SSA Parser
// ===========================================================================
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

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("  \n\n  ").cues).toHaveLength(0);
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

  it("strips \\n (lowercase) as newlines too", () => {
    const input = [
      "[Script Info]",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,First\\nsecond",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("First\nsecond");
  });

  it("includes header sections in metadata.rawHeader", () => {
    const result = parser.parse(minimalASS);
    const rawHeader = result.metadata.rawHeader as string;
    expect(rawHeader).toContain("[Script Info]");
    expect(rawHeader).toContain("[V4+ Styles]");
    expect(rawHeader).not.toContain("[Events]");
  });

  it("detects SSA format (V4 Styles) vs ASS (V4+ Styles)", () => {
    const ssaInput = [
      "[Script Info]",
      "ScriptType: v4.00",
      "",
      "[V4 Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,Hello",
    ].join("\n");

    const result = parser.parse(ssaInput);
    expect(result.metadata.format).toBe("ssa");
  });

  it("handles text containing commas", () => {
    const input = [
      "[Script Info]",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,Hello, world, and everyone",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Hello, world, and everyone");
  });

  it("strips multiple override tags", () => {
    const input = [
      "[Script Info]",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,{\\an8}{\\pos(320,50)}{\\fad(1000,0)}Top positioned text",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Top positioned text");
  });

  it("extracts styles from metadata", () => {
    const result = parser.parse(minimalASS);
    expect(result.metadata.styles).toBeDefined();
    expect(result.metadata.styles).toContain("Style: Default,Arial,20");
  });

  it("handles BOM prefix", () => {
    const input = "\uFEFF" + minimalASS;
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("handles Windows line endings", () => {
    const input = minimalASS.replace(/\n/g, "\r\n");
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("skips Comment lines in Events", () => {
    const input = [
      "[Script Info]",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Comment: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,This is a comment",
      "Dialogue: 0,0:00:05.00,0:00:08.00,Default,,0000,0000,0000,,Real dialogue",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("Real dialogue");
  });

  it("handles centisecond timestamp precision", () => {
    const input = [
      "[Script Info]",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,1:23:45.67,1:23:50.00,Default,,0000,0000,0000,,Precise timing",
    ].join("\n");

    const result = parser.parse(input);
    // 1*3600000 + 23*60000 + 45*1000 + 67*10 = 5025670
    expect(result.cues[0].startMs).toBe(5025670);
    expect(result.cues[0].endMs).toBe(5030000);
  });
});

// ===========================================================================
// SUB (MicroDVD) Parser
// ===========================================================================
describe("SUB parser", () => {
  const parser = getParser("sub");
  const DEFAULT_FPS = 23.976;

  it("parses frame-based lines and converts to ms at 23.976fps", () => {
    const input = "{0}{100}Hello";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.metadata.format).toBe("sub");
    expect(result.cues[0].startMs).toBe(0);
    expect(result.cues[0].endMs).toBe(Math.round((100 / DEFAULT_FPS) * 1000));
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[0].rawText).toBe("{0}{100}Hello");
  });

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("  \n\n  ").cues).toHaveLength(0);
  });

  it("converts pipe characters to newlines", () => {
    const input = "{0}{100}Line one|Line two|Line three";
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Line one\nLine two\nLine three");
  });

  it("parses multiple cues", () => {
    const input = ["{0}{100}First", "{200}{400}Second", "{500}{700}Third"].join(
      "\n",
    );

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(3);
    expect(result.cues[2].startMs).toBe(Math.round((500 / DEFAULT_FPS) * 1000));
  });

  it("skips lines that do not match the frame pattern", () => {
    const input = [
      "This is a comment line",
      "{0}{100}Valid cue",
      "Another invalid line",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles special characters in text", () => {
    const input = '{0}{100}Caf\u00e9 & "quotes" <angle>';
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe('Caf\u00e9 & "quotes" <angle>');
  });

  it("handles BOM prefix", () => {
    const input = "\uFEFF{0}{100}With BOM";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles Windows line endings", () => {
    const input = "{0}{100}First\r\n{200}{400}Second";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("handles large frame numbers", () => {
    const input = "{100000}{200000}Far into the movie";
    const result = parser.parse(input);
    expect(result.cues[0].startMs).toBe(
      Math.round((100000 / DEFAULT_FPS) * 1000),
    );
  });

  it("preserves rawText with pipe characters intact", () => {
    const input = "{0}{100}Line one|Line two";
    const result = parser.parse(input);
    expect(result.cues[0].rawText).toBe("{0}{100}Line one|Line two");
  });
});

// ===========================================================================
// SMI Parser
// ===========================================================================
describe("SMI parser", () => {
  const parser = getParser("smi");

  it("parses SYNC-based content", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>Hello",
      "<SYNC Start=4000>&nbsp;",
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

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("  \n\n  ").cues).toHaveLength(0);
  });

  it("strips HTML tags from text", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      '<SYNC Start=1000><P Class=ENCC><font color="#FFFFFF">Hello <b>world</b></font>',
      "<SYNC Start=4000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Hello world");
  });

  it("converts <br> to newlines", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>Line one<br>Line two<BR/>Line three",
      "<SYNC Start=4000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Line one\nLine two\nLine three");
  });

  it("decodes HTML entities", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>&lt;angle&gt; &amp; stuff",
      "<SYNC Start=4000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("<angle> & stuff");
  });

  it("skips empty/nbsp clear markers", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>First cue",
      "<SYNC Start=3000>&nbsp;",
      "<SYNC Start=5000>Second cue",
      "<SYNC Start=8000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
    expect(result.cues[0].text).toBe("First cue");
    expect(result.cues[0].endMs).toBe(3000);
    expect(result.cues[1].text).toBe("Second cue");
    expect(result.cues[1].endMs).toBe(8000);
  });

  it("extracts title from TITLE tag", () => {
    const input = [
      "<SAMI>",
      "<HEAD>",
      "<TITLE>My Subtitle File</TITLE>",
      "</HEAD>",
      "<BODY>",
      "<SYNC Start=1000>Hello",
      "<SYNC Start=4000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.title).toBe("My Subtitle File");
  });

  it("assigns default endMs of startMs + 3000 for last cue without clear marker", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>Only cue, no clear",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].endMs).toBe(4000);
  });

  it("handles case-insensitive SYNC tags", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<sync start=1000>Lower case",
      "<Sync Start=4000>&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].text).toBe("Lower case");
  });

  it("handles BOM prefix", () => {
    const input =
      "\uFEFF<SAMI><BODY><SYNC Start=1000>BOM<SYNC Start=4000>&nbsp;</BODY></SAMI>";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles Windows line endings", () => {
    const input =
      "<SAMI>\r\n<BODY>\r\n<SYNC Start=1000>Hello\r\n<SYNC Start=4000>&nbsp;\r\n</BODY>\r\n</SAMI>";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });
});

// ===========================================================================
// MPL (MPL2) Parser
// ===========================================================================
describe("MPL parser", () => {
  const parser = getParser("mpl");

  it("parses decisecond-based lines and converts to ms", () => {
    const input = "[100][200]Hello";
    const result = parser.parse(input);
    expect(result.metadata.format).toBe("mpl");
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].startMs).toBe(10000);
    expect(result.cues[0].endMs).toBe(20000);
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[0].rawText).toBe("[100][200]Hello");
  });

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("  \n\n  ").cues).toHaveLength(0);
  });

  it("converts pipe characters to newlines", () => {
    const input = "[100][200]Line one|Line two|Line three";
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Line one\nLine two\nLine three");
  });

  it("parses multiple cues", () => {
    const input = [
      "[100][200]First",
      "[300][400]Second",
      "[500][600]Third",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(3);
    expect(result.cues[0].startMs).toBe(10000);
    expect(result.cues[1].startMs).toBe(30000);
    expect(result.cues[2].startMs).toBe(50000);
  });

  it("skips lines that do not match the MPL pattern", () => {
    const input = [
      "// comment line",
      "[100][200]Valid cue",
      "random garbage",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles special characters in text", () => {
    const input = '[100][200]Caf\u00e9 & "quotes"';
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe('Caf\u00e9 & "quotes"');
  });

  it("handles BOM prefix", () => {
    const input = "\uFEFF[100][200]With BOM";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles Windows line endings", () => {
    const input = "[100][200]First\r\n[300][400]Second";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("preserves rawText with pipe characters intact", () => {
    const input = "[100][200]Line one|Line two";
    const result = parser.parse(input);
    expect(result.cues[0].rawText).toBe("[100][200]Line one|Line two");
  });

  it("handles zero-valued timestamps", () => {
    const input = "[0][100]From start";
    const result = parser.parse(input);
    expect(result.cues[0].startMs).toBe(0);
    expect(result.cues[0].endMs).toBe(10000);
  });

  it("handles large decisecond values", () => {
    const input = "[36000][72000]One hour to two hours";
    const result = parser.parse(input);
    expect(result.cues[0].startMs).toBe(3600000);
    expect(result.cues[0].endMs).toBe(7200000);
  });
});

// ===========================================================================
// TXT (TMPlayer and numbered format) Parser
// ===========================================================================
describe("TXT parser", () => {
  const parser = getParser("txt");

  it("parses TMPlayer format with colon separator", () => {
    const input = ["00:01:00:Hello", "00:02:00:World"].join("\n");

    const result = parser.parse(input);
    expect(result.metadata.format).toBe("txt");
    expect(result.cues).toHaveLength(2);
    expect(result.cues[0].startMs).toBe(60000);
    expect(result.cues[0].endMs).toBe(120000);
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[1].startMs).toBe(120000);
    expect(result.cues[1].endMs).toBe(123000);
    expect(result.cues[1].text).toBe("World");
  });

  it("returns 0 cues for empty input", () => {
    expect(parser.parse("").cues).toHaveLength(0);
  });

  it("returns 0 cues for whitespace-only input", () => {
    expect(parser.parse("  \n\n  ").cues).toHaveLength(0);
  });

  it("parses TMPlayer format with equals separator", () => {
    const input = ["00:01:00=Hello", "00:02:00=World"].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
    expect(result.cues[0].startMs).toBe(60000);
    expect(result.cues[0].text).toBe("Hello");
  });

  it("adjusts end times so each cue ends when the next starts", () => {
    const input = ["00:00:10:First", "00:00:20:Second", "00:00:30:Third"].join(
      "\n",
    );

    const result = parser.parse(input);
    expect(result.cues[0].endMs).toBe(20000);
    expect(result.cues[1].endMs).toBe(30000);
    expect(result.cues[2].endMs).toBe(33000); // last gets +3000
  });

  it("assigns default 3000ms duration to single cue", () => {
    const input = "00:01:00:Solo cue";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
    expect(result.cues[0].startMs).toBe(60000);
    expect(result.cues[0].endMs).toBe(63000);
  });

  it("converts pipe characters to newlines in TMPlayer format", () => {
    const input = "00:00:10:Line one|Line two";
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe("Line one\nLine two");
  });

  it("parses numbered format (N HH:MM:SS text)", () => {
    const input = ["1 00:01:00 Hello", "2 00:02:00 World"].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
    expect(result.cues[0].startMs).toBe(60000);
    expect(result.cues[0].text).toBe("Hello");
    expect(result.cues[0].endMs).toBe(120000); // adjusted to next start
    expect(result.cues[1].endMs).toBe(123000); // last gets +3000
  });

  it("handles BOM prefix", () => {
    const input = "\uFEFF00:01:00:BOM text";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(1);
  });

  it("handles Windows line endings", () => {
    const input = "00:01:00:First\r\n00:02:00:Second";
    const result = parser.parse(input);
    expect(result.cues).toHaveLength(2);
  });

  it("handles special characters in TMPlayer text", () => {
    const input = '00:01:00:Caf\u00e9 & "quotes" <angle>';
    const result = parser.parse(input);
    expect(result.cues[0].text).toBe('Caf\u00e9 & "quotes" <angle>');
  });

  it("returns 0 cues when first line does not match any known format", () => {
    // When the first line is not TMPlayer format, it tries numbered format.
    // Lines like "00:01:00:Valid cue" do not match the numbered pattern either,
    // so the parser returns nothing.
    const input = [
      "This is a comment",
      "00:01:00:Valid cue",
      "Another comment",
    ].join("\n");

    const result = parser.parse(input);
    expect(result.cues).toHaveLength(0);
  });

  it("correctly auto-detects TMPlayer vs numbered based on first line", () => {
    // TMPlayer: starts with timestamp
    const tmInput = "00:00:10:Hello";
    const tmResult = parser.parse(tmInput);
    expect(tmResult.cues[0].text).toBe("Hello");

    // Numbered: starts with a number then timestamp
    const numInput = "1 00:00:10 Hello";
    const numResult = parser.parse(numInput);
    expect(numResult.cues[0].text).toBe("Hello");
  });
});
