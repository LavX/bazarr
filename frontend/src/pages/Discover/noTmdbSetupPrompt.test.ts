import { describe, expect, it } from "vitest";

// Every application source file, read as text. Discover always runs on the
// built-in TMDB key (the server falls back to it even when a saved key is
// rejected), so no screen may ask the reader to connect or set up TMDB. A
// rendered test only covers the states it mounts; reading the source covers
// the ones nobody thought to mount.
const sources = import.meta.glob<string>(
  [
    "../../**/*.{ts,tsx}",
    "!../../**/*.test.{ts,tsx}",
    "!../../**/__tests__/**",
    "!../../tests/**",
  ],
  { query: "?raw", import: "default", eager: true },
);

const PROMPTS = [
  /connect\s+tmdb/i,
  /set\s+up\s+tmdb/i,
  /set\s+up\s+discover/i,
  /set\s+up\s+(recent episodes|digital releases)/i,
  /check\s+the\s+tmdb\s+key/i,
  /explore\s+beyond\s+your\s+library/i,
];

describe("TMDB setup prompts", () => {
  it("reads the application sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(100);
    expect(Object.keys(sources)).toContain("./Trending.tsx");
  });

  it("never asks the reader to connect or set up TMDB", () => {
    const found = Object.entries(sources).flatMap(([file, text]) =>
      PROMPTS.filter((prompt) => prompt.test(text)).map(
        (prompt) => `${file}: ${prompt}`,
      ),
    );
    expect(found).toEqual([]);
  });
});
