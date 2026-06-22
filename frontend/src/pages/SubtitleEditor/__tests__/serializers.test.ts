import { beforeEach, describe, expect, it, vi } from "vitest";
import { getParser } from "@/pages/SubtitleEditor/parsers";
import { getSerializer } from "@/pages/SubtitleEditor/serializers";
import type { Cue, ParseResult } from "@/pages/SubtitleEditor/types";

// Mock crypto.randomUUID since it may not be available in test env
beforeEach(() => {
  let counter = 0;
  vi.stubGlobal("crypto", {
    randomUUID: () => `test-uuid-${++counter}`,
  });
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function makeCue(
  startMs: number,
  endMs: number,
  text: string,
  rawText?: string,
): Cue {
  return { id: crypto.randomUUID(), startMs, endMs, text, rawText };
}

function makeResult(
  format: ParseResult["metadata"]["format"],
  cues: Cue[],
  metadata?: Partial<ParseResult["metadata"]>,
): ParseResult {
  return {
    metadata: { format, ...metadata },
    cues,
  };
}

// ---------------------------------------------------------------------------
// SRT Serializer
// ---------------------------------------------------------------------------
describe("SRT serializer", () => {
  const serializer = getSerializer("srt");

  it("serializes cues with correct SRT numbering and timestamp format", () => {
    const result = makeResult("srt", [
      makeCue(1000, 2500, "Hello world"),
      makeCue(60000, 63400, "Second cue"),
    ]);

    const output = serializer.serialize(result);
    const lines = output.split("\n");

    // First cue block
    expect(lines[0]).toBe("1");
    expect(lines[1]).toBe("00:00:01,000 --> 00:00:02,500");
    expect(lines[2]).toBe("Hello world");

    // Blank line separator
    expect(lines[3]).toBe("");

    // Second cue block
    expect(lines[4]).toBe("2");
    expect(lines[5]).toBe("00:01:00,000 --> 00:01:03,400");
    expect(lines[6]).toBe("Second cue");
  });

  it("formats timestamps with comma separator (not dot)", () => {
    const result = makeResult("srt", [makeCue(3661234, 3662000, "Test")]);
    const output = serializer.serialize(result);
    expect(output).toContain("01:01:01,234 --> 01:01:02,000");
  });

  it("handles multi-line cue text", () => {
    const result = makeResult("srt", [
      makeCue(0, 1000, "Line one\nLine two\nLine three"),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one\nLine two\nLine three");
  });

  it("returns empty string for empty cues array", () => {
    const result = makeResult("srt", []);
    expect(serializer.serialize(result)).toBe("");
  });

  it("preserves special characters", () => {
    const result = makeResult("srt", [
      makeCue(0, 1000, 'Caf\u00e9 & "quotes" <tag> \u2603'),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain('Caf\u00e9 & "quotes" <tag> \u2603');
  });

  it("round-trips: parse -> serialize -> parse produces same cues", () => {
    const input = [
      "1",
      "00:00:01,000 --> 00:00:02,500",
      "Hello world",
      "",
      "2",
      "00:01:00,000 --> 00:01:03,400",
      "Multi-line",
      "cue text",
    ].join("\n");

    const parser = getParser("srt");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].endMs).toBe(parsed.cues[i].endMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });

  it("pads hours, minutes, seconds, and milliseconds correctly", () => {
    const result = makeResult("srt", [makeCue(0, 500, "Pad test")]);
    const output = serializer.serialize(result);
    expect(output).toContain("00:00:00,000 --> 00:00:00,500");
  });

  it("handles large timestamps (hours > 0)", () => {
    // 2 hours, 30 minutes, 15 seconds, 123ms
    const ms = 2 * 3600000 + 30 * 60000 + 15 * 1000 + 123;
    const result = makeResult("srt", [makeCue(ms, ms + 1000, "Late cue")]);
    const output = serializer.serialize(result);
    expect(output).toContain("02:30:15,123");
  });
});

// ---------------------------------------------------------------------------
// VTT Serializer
// ---------------------------------------------------------------------------
describe("VTT serializer", () => {
  const serializer = getSerializer("vtt");

  it("includes WEBVTT header by default", () => {
    const result = makeResult("vtt", [makeCue(1000, 2000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output.startsWith("WEBVTT")).toBe(true);
  });

  it("uses custom rawHeader when provided", () => {
    const result = makeResult("vtt", [makeCue(1000, 2000, "Hello")], {
      rawHeader: "WEBVTT - My Custom Header",
    });
    const output = serializer.serialize(result);
    expect(output.startsWith("WEBVTT - My Custom Header")).toBe(true);
  });

  it("uses dot separator for timestamps (not comma)", () => {
    const result = makeResult("vtt", [makeCue(1234, 5678, "Test")]);
    const output = serializer.serialize(result);
    expect(output).toContain("00:00:01.234 --> 00:00:05.678");
  });

  it("serializes multiple cues separated by blank lines", () => {
    const result = makeResult("vtt", [
      makeCue(1000, 2000, "First"),
      makeCue(3000, 4000, "Second"),
    ]);
    const output = serializer.serialize(result);
    const blocks = output.split("\n\n");
    expect(blocks).toHaveLength(3); // header + 2 cue blocks
  });

  it("handles multi-line text", () => {
    const result = makeResult("vtt", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one\nLine two");
  });

  it("returns only header for empty cues", () => {
    const result = makeResult("vtt", []);
    const output = serializer.serialize(result);
    expect(output).toBe("WEBVTT\n\n");
  });

  it("preserves special characters", () => {
    const result = makeResult("vtt", [
      makeCue(0, 1000, "\u00e9\u00e8\u00ea & <b>bold</b>"),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain("\u00e9\u00e8\u00ea & <b>bold</b>");
  });

  it("round-trips: parse -> serialize -> parse produces same cues", () => {
    const input = [
      "WEBVTT",
      "",
      "00:00:01.000 --> 00:00:04.000",
      "Hello world",
      "",
      "00:01:00.000 --> 00:01:03.500",
      "Second line\nWith break",
    ].join("\n");

    const parser = getParser("vtt");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].endMs).toBe(parsed.cues[i].endMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// ASS Serializer
// ---------------------------------------------------------------------------
describe("ASS serializer", () => {
  const serializer = getSerializer("ass");

  const customHeader = [
    "[Script Info]",
    "Title: Test Script",
    "ScriptType: v4.00+",
    "",
    "[V4+ Styles]",
    "Format: Name, Fontname, Fontsize",
    "Style: Default,Arial,20",
  ].join("\n");

  it("uses custom rawHeader when provided", () => {
    const result = makeResult("ass", [makeCue(1000, 4000, "Hello")], {
      rawHeader: customHeader,
    });
    const output = serializer.serialize(result);
    expect(output).toContain("[Script Info]");
    expect(output).toContain("Title: Test Script");
  });

  it("uses minimal header when no rawHeader", () => {
    const result = makeResult("ass", [makeCue(1000, 4000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output).toContain("[Script Info]");
    expect(output).toContain("ScriptType: v4.00+");
  });

  it("includes [Events] section with Format line", () => {
    const result = makeResult("ass", [makeCue(1000, 4000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output).toContain("[Events]");
    expect(output).toContain(
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    );
  });

  it("formats ASS timestamps with single-digit hours and centiseconds", () => {
    // ASS format: H:MM:SS.CC (centiseconds, not milliseconds)
    const result = makeResult("ass", [makeCue(3661230, 3662000, "Test")]);
    const output = serializer.serialize(result);
    // 3661230ms = 1h 1m 1s 230ms = 1:01:01.23 (23 centiseconds)
    expect(output).toContain("1:01:01.23");
  });

  it("generates Dialogue lines for new cues (no rawText)", () => {
    const result = makeResult("ass", [makeCue(1000, 4000, "Hello world")]);
    const output = serializer.serialize(result);
    expect(output).toContain(
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0,0,0,,Hello world",
    );
  });

  it("converts newlines to \\N in dialogue text", () => {
    const result = makeResult("ass", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one\\NLine two");
    // The serialized text should NOT have literal newlines within the dialogue
    const dialogueLine = output
      .split("\n")
      .find((l) => l.startsWith("Dialogue:"));
    expect(dialogueLine).not.toContain("\nLine two");
  });

  it("patches existing rawText lines, preserving style/margins", () => {
    const rawLine =
      "Dialogue: 0,0:00:01.00,0:00:04.00,CustomStyle,Speaker,0010,0020,0030,karaoke,Original text";
    const cue = makeCue(2000, 5000, "Updated text", rawLine);
    const result = makeResult("ass", [cue]);
    const output = serializer.serialize(result);

    const dialogueLine = output
      .split("\n")
      .find((l) => l.startsWith("Dialogue:"));
    expect(dialogueLine).toBeDefined();
    // Timing updated
    expect(dialogueLine).toContain("0:00:02.00");
    expect(dialogueLine).toContain("0:00:05.00");
    // Text updated
    expect(dialogueLine).toContain("Updated text");
    // Style, Name, Margins, Effect preserved
    expect(dialogueLine).toContain("CustomStyle");
    expect(dialogueLine).toContain("Speaker");
    expect(dialogueLine).toContain("0010");
    expect(dialogueLine).toContain("karaoke");
  });

  it("returns empty cues with just header and events format", () => {
    const result = makeResult("ass", []);
    const output = serializer.serialize(result);
    expect(output).toContain("[Events]");
    expect(output).toContain("Format:");
    expect(output).not.toContain("Dialogue:");
  });

  it("preserves special characters in text", () => {
    const result = makeResult("ass", [
      makeCue(0, 1000, 'Caf\u00e9 & "quotes"'),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain('Caf\u00e9 & "quotes"');
  });

  it("clamps negative timestamps to 0", () => {
    const result = makeResult("ass", [makeCue(-500, 1000, "Negative start")]);
    const output = serializer.serialize(result);
    expect(output).toContain("0:00:00.00");
  });

  it("round-trips: parse -> serialize -> parse produces same cues", () => {
    const input = [
      "[Script Info]",
      "Title: Round Trip Test",
      "ScriptType: v4.00+",
      "",
      "[V4+ Styles]",
      "Format: Name, Fontname, Fontsize",
      "Style: Default,Arial,20",
      "",
      "[Events]",
      "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
      "Dialogue: 0,0:00:01.00,0:00:04.00,Default,,0000,0000,0000,,Hello world",
      "Dialogue: 0,0:01:00.50,0:01:03.00,Default,,0000,0000,0000,,Second line",
    ].join("\n");

    const parser = getParser("ass");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].endMs).toBe(parsed.cues[i].endMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// SUB (MicroDVD) Serializer
// ---------------------------------------------------------------------------
describe("SUB serializer", () => {
  const serializer = getSerializer("sub");
  const FPS = 23.976;

  it("serializes cues with frame-based timestamps in curly braces", () => {
    const result = makeResult("sub", [makeCue(0, 4170, "Hello")]);
    const output = serializer.serialize(result);
    // 0ms = frame 0, 4170ms at 23.976fps = Math.round(4170 * 23.976 / 1000) = 100
    const startFrame = Math.round((0 * FPS) / 1000);
    const endFrame = Math.round((4170 * FPS) / 1000);
    expect(output).toBe(`{${startFrame}}{${endFrame}}Hello`);
  });

  it("converts newlines to pipe character", () => {
    const result = makeResult("sub", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one|Line two");
    expect(output).not.toContain("\nLine two");
  });

  it("rebuilds from edited fields, ignoring stale rawText", () => {
    const rawLine = "{100}{200}Original text with|pipe";
    const cue = makeCue(4170, 8340, "Different text", rawLine);
    const result = makeResult("sub", [cue]);
    const output = serializer.serialize(result);
    // The edited timing/text must be honored, not the original rawText.
    // 4170ms -> frame 100, 8340ms -> frame 200 at 23.976fps.
    const startFrame = Math.round((4170 * FPS) / 1000);
    const endFrame = Math.round((8340 * FPS) / 1000);
    expect(output).toBe(`{${startFrame}}{${endFrame}}Different text`);
  });

  it("returns empty string for empty cues", () => {
    const result = makeResult("sub", []);
    expect(serializer.serialize(result)).toBe("");
  });

  it("preserves special characters", () => {
    const result = makeResult("sub", [
      makeCue(0, 1000, '\u00e9\u00e8 & "test"'),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain('\u00e9\u00e8 & "test"');
  });

  it("serializes multiple cues separated by newlines", () => {
    const result = makeResult("sub", [
      makeCue(0, 1000, "First"),
      makeCue(2000, 3000, "Second"),
    ]);
    const output = serializer.serialize(result);
    const lines = output.split("\n");
    expect(lines).toHaveLength(2);
  });

  it("round-trips: parse -> serialize -> parse produces same cues (within frame precision)", () => {
    const input = ["{0}{100}Hello world", "{200}{300}Second cue"].join("\n");

    const parser = getParser("sub");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      // Frame-based formats lose sub-frame precision, so check within 1 frame
      const frameDurationMs = 1000 / FPS;
      expect(
        Math.abs(reparsed.cues[i].startMs - parsed.cues[i].startMs),
      ).toBeLessThan(frameDurationMs);
      expect(
        Math.abs(reparsed.cues[i].endMs - parsed.cues[i].endMs),
      ).toBeLessThan(frameDurationMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// SMI Serializer
// ---------------------------------------------------------------------------
describe("SMI serializer", () => {
  const serializer = getSerializer("smi");

  it("wraps output in <SAMI><BODY>...</BODY></SAMI> tags", () => {
    const result = makeResult("smi", [makeCue(1000, 4000, "Hello")]);
    const output = serializer.serialize(result);
    const lines = output.split("\n");
    expect(lines[0]).toBe("<SAMI>");
    expect(lines[1]).toBe("<BODY>");
    expect(lines[lines.length - 2]).toBe("</BODY>");
    expect(lines[lines.length - 1]).toBe("</SAMI>");
  });

  it("uses SYNC Start tags with millisecond values", () => {
    const result = makeResult("smi", [makeCue(1000, 4000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output).toContain("<SYNC Start=1000>");
    expect(output).toContain("<SYNC Start=4000>");
  });

  it("inserts &nbsp; blank after each cue end time", () => {
    const result = makeResult("smi", [makeCue(1000, 4000, "Hello")]);
    const output = serializer.serialize(result);
    const lines = output.split("\n");
    // After the end SYNC tag, there should be &nbsp;
    const endSyncIdx = lines.findIndex((l) => l === "<SYNC Start=4000>");
    expect(lines[endSyncIdx + 1]).toBe("&nbsp;");
  });

  it("converts newlines to <br> tags", () => {
    const result = makeResult("smi", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one<br>Line two");
  });

  it("handles multiple cues in sequence", () => {
    const result = makeResult("smi", [
      makeCue(1000, 2000, "First"),
      makeCue(3000, 4000, "Second"),
    ]);
    const output = serializer.serialize(result);
    // Should have 4 SYNC tags total (start+end for each cue)
    const syncMatches = output.match(/<SYNC Start=\d+>/g);
    expect(syncMatches).toHaveLength(4);
  });

  it("returns wrapper tags only for empty cues", () => {
    const result = makeResult("smi", []);
    const output = serializer.serialize(result);
    expect(output).toBe("<SAMI>\n<BODY>\n</BODY>\n</SAMI>");
  });

  it("preserves special characters (except newlines)", () => {
    const result = makeResult("smi", [
      makeCue(0, 1000, '\u00e9\u00e8 & "quotes"'),
    ]);
    const output = serializer.serialize(result);
    expect(output).toContain('\u00e9\u00e8 & "quotes"');
  });

  it("round-trips: parse -> serialize -> parse produces same cues", () => {
    const input = [
      "<SAMI>",
      "<BODY>",
      "<SYNC Start=1000>",
      "Hello world",
      "<SYNC Start=4000>",
      "&nbsp;",
      "<SYNC Start=5000>",
      "Second cue",
      "<SYNC Start=8000>",
      "&nbsp;",
      "</BODY>",
      "</SAMI>",
    ].join("\n");

    const parser = getParser("smi");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].endMs).toBe(parsed.cues[i].endMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// MPL Serializer
// ---------------------------------------------------------------------------
describe("MPL serializer", () => {
  const serializer = getSerializer("mpl");

  it("serializes cues with decisecond timestamps in square brackets", () => {
    // 10000ms = 100 deciseconds, 20000ms = 200 deciseconds
    const result = makeResult("mpl", [makeCue(10000, 20000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output).toBe("[100][200]Hello");
  });

  it("converts newlines to pipe character", () => {
    const result = makeResult("mpl", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one|Line two");
  });

  it("rebuilds from edited fields, ignoring stale rawText", () => {
    const rawLine = "[100][200]Original|text";
    const cue = makeCue(10000, 20000, "Different text", rawLine);
    const result = makeResult("mpl", [cue]);
    const output = serializer.serialize(result);
    // The edited timing/text must be honored, not the original rawText.
    // 10000ms -> 100 ds, 20000ms -> 200 ds.
    expect(output).toBe("[100][200]Different text");
  });

  it("returns empty string for empty cues", () => {
    const result = makeResult("mpl", []);
    expect(serializer.serialize(result)).toBe("");
  });

  it("preserves special characters", () => {
    const result = makeResult("mpl", [makeCue(0, 1000, 'Caf\u00e9 & "test"')]);
    const output = serializer.serialize(result);
    expect(output).toContain('Caf\u00e9 & "test"');
  });

  it("rounds milliseconds to nearest decisecond", () => {
    // 1050ms = 10.5 deciseconds, rounds to 11
    // 1049ms = 10.49 deciseconds, rounds to 10
    const result = makeResult("mpl", [makeCue(1050, 1949, "Test")]);
    const output = serializer.serialize(result);
    expect(output).toBe("[11][19]Test");
  });

  it("serializes multiple cues separated by newlines", () => {
    const result = makeResult("mpl", [
      makeCue(10000, 20000, "First"),
      makeCue(30000, 40000, "Second"),
    ]);
    const output = serializer.serialize(result);
    const lines = output.split("\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toBe("[100][200]First");
    expect(lines[1]).toBe("[300][400]Second");
  });

  it("round-trips: parse -> serialize -> parse produces same cues (within decisecond precision)", () => {
    const input = ["[100][200]Hello world", "[300][400]Second cue"].join("\n");

    const parser = getParser("mpl");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].endMs).toBe(parsed.cues[i].endMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// TXT (TMPlayer) Serializer
// ---------------------------------------------------------------------------
describe("TXT serializer", () => {
  const serializer = getSerializer("txt");

  it("serializes cues in HH:MM:SS:text format", () => {
    const result = makeResult("txt", [makeCue(60000, 63000, "Hello")]);
    const output = serializer.serialize(result);
    expect(output).toBe("00:01:00:Hello");
  });

  it("only uses start time (no end time in format)", () => {
    const result = makeResult("txt", [makeCue(60000, 120000, "Hello")]);
    const output = serializer.serialize(result);
    // Should only have one timestamp
    const colonCount = output.split(":").length - 1;
    // HH:MM:SS:text = 3 colons
    expect(colonCount).toBe(3);
  });

  it("converts newlines to pipe character", () => {
    const result = makeResult("txt", [makeCue(0, 1000, "Line one\nLine two")]);
    const output = serializer.serialize(result);
    expect(output).toContain("Line one|Line two");
  });

  it("returns empty string for empty cues", () => {
    const result = makeResult("txt", []);
    expect(serializer.serialize(result)).toBe("");
  });

  it("preserves special characters", () => {
    const result = makeResult("txt", [makeCue(0, 1000, 'Caf\u00e9 & "test"')]);
    const output = serializer.serialize(result);
    expect(output).toContain('Caf\u00e9 & "test"');
  });

  it("pads hours, minutes, and seconds to 2 digits", () => {
    const result = makeResult("txt", [makeCue(1000, 2000, "Test")]);
    const output = serializer.serialize(result);
    expect(output).toBe("00:00:01:Test");
  });

  it("handles large timestamps", () => {
    // 2h 30m 15s
    const ms = 2 * 3600000 + 30 * 60000 + 15 * 1000;
    const result = makeResult("txt", [makeCue(ms, ms + 1000, "Late")]);
    const output = serializer.serialize(result);
    expect(output).toBe("02:30:15:Late");
  });

  it("truncates milliseconds (no sub-second precision)", () => {
    const result = makeResult("txt", [makeCue(1999, 3000, "Truncated")]);
    const output = serializer.serialize(result);
    // 1999ms = 1 second (truncated), not rounded
    expect(output).toBe("00:00:01:Truncated");
  });

  it("serializes multiple cues separated by newlines", () => {
    const result = makeResult("txt", [
      makeCue(60000, 63000, "First"),
      makeCue(120000, 123000, "Second"),
    ]);
    const output = serializer.serialize(result);
    const lines = output.split("\n");
    expect(lines).toHaveLength(2);
    expect(lines[0]).toBe("00:01:00:First");
    expect(lines[1]).toBe("00:02:00:Second");
  });

  it("round-trips: parse -> serialize -> parse preserves text and start time", () => {
    const input = ["00:01:00:Hello world", "00:02:00:Second cue"].join("\n");

    const parser = getParser("txt");
    const parsed = parser.parse(input);
    const serialized = serializer.serialize(parsed);
    const reparsed = parser.parse(serialized);

    expect(reparsed.cues).toHaveLength(parsed.cues.length);
    for (let i = 0; i < parsed.cues.length; i++) {
      expect(reparsed.cues[i].startMs).toBe(parsed.cues[i].startMs);
      expect(reparsed.cues[i].text).toBe(parsed.cues[i].text);
    }
  });
});

// ---------------------------------------------------------------------------
// getSerializer coverage
// ---------------------------------------------------------------------------
describe("getSerializer", () => {
  it("returns the same serializer for 'ssa' and 'ass'", () => {
    expect(getSerializer("ssa")).toBe(getSerializer("ass"));
  });

  it("returns a serializer for every supported format", () => {
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
    for (const format of formats) {
      const s = getSerializer(format);
      expect(s).toBeDefined();
      expect(typeof s.serialize).toBe("function");
    }
  });
});
