import { describe, expect, it } from "vitest";

// Every source file of the subtitle editor, read as text. The editor sends
// Bazarr's API key in the X-API-KEY header. A key in a URL ends up in reverse
// proxy access logs and makes the server log a deprecation warning on every
// request. The rendered tests cover the requests they mount; reading the
// source also covers a new fetch that nobody wrote a test for.
const sources = import.meta.glob<string>(
  ["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../__tests__/**"],
  { query: "?raw", import: "default", eager: true },
);

const KEY_IN_URL = [
  // The query parameter the server reads (`apikey=`, "apikey", apikey:).
  /\bapikey\b/,
  // The key's value interpolated into a template string.
  /\$\{[^}]*\bapiKey\b[^}]*\}/,
];

// Native HLS on Safari and iOS cannot add a header to the playlist and
// segment requests the browser makes itself, so that one source still carries
// the key until it gets a stream token of its own.
const NATIVE_HLS_SOURCE =
  "../VideoPreview.tsx: video.src = `${hlsUrl}${separator}apikey=${encodeURIComponent(apiKey)}`;";

describe("API key in editor URLs", () => {
  it("reads the editor sources", () => {
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining([
        "../EditorPage.tsx",
        "../TimingToolsPanel.tsx",
        "../VideoPreview.tsx",
        "../WaveformTimeline.tsx",
        "../editorScope.ts",
      ]),
    );
    expect(
      Object.keys(sources).filter((file) => file.includes("__tests__")),
    ).toEqual([]);
  });

  it("never builds the API key into a URL outside native HLS", () => {
    const found = Object.entries(sources).flatMap(([file, text]) =>
      text
        .split("\n")
        .filter((line) => KEY_IN_URL.some((pattern) => pattern.test(line)))
        .map((line) => `${file}: ${line.trim()}`),
    );
    expect(found).toEqual([NATIVE_HLS_SOURCE]);
  });
});
