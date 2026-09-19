import { describe, expect, it } from "vitest";
import {
  bodyLabel,
  classifyAnnouncement,
  linkHost,
  splitAnnouncementText,
  splitLinks,
} from "./announcementText";

// The live feed's own entries. The rules below are calibrated against what the
// announcements actually say, so a rewrite that only suits invented samples
// still fails here.
const PROVIDERS_JOINED =
  "7 new subtitle providers have joined the Provider Hub! Install from the marketplace without updating Bazarr+: BollyNook (Indian and Bollywood movie subtitles in 40+ languages), iSubtitles, SubHD (Chinese-first movie and episode subtitles). Browse Subtitle Hub > Marketplace to install. Catalog: github.com/LavX/bazarr-provider-catalog";

const V230 =
  "Bazarr+ v2.3.0 is live! This release introduces the Provider Hub, a built-in marketplace for installable subtitle providers. First installable provider: SubtitleCat, the top-voted request on the upstream feature tracker (https://bazarr.featureupvote.com/suggestions/54294/provider-request-subtitle-cat). Browse the marketplace under Subtitle Hub > Marketplace.";

const FANSUBS =
  "fansubs.ru has joined the Provider Hub! A Russian fan-subtitle source is now installable from the marketplace. Open Subtitle Hub > Marketplace to install, no Bazarr+ update required.";

describe("classifyAnnouncement", () => {
  it("files a version note under releases even though it introduces the Hub", () => {
    expect(classifyAnnouncement(V230)).toBe("release");
  });

  it("files a Hub entry under providers when no version is named", () => {
    expect(classifyAnnouncement(PROVIDERS_JOINED)).toBe("provider");
    expect(classifyAnnouncement(FANSUBS)).toBe("provider");
  });

  it("files anything else as a plain notice", () => {
    expect(classifyAnnouncement("Python 3.9 support is ending.")).toBe(
      "notice",
    );
  });
});

describe("splitAnnouncementText", () => {
  it("takes the lead sentence as the headline and keeps the rest as body", () => {
    const parts = splitAnnouncementText(PROVIDERS_JOINED);

    expect(parts.headline).toBe(
      "7 new subtitle providers have joined the Provider Hub!",
    );
    expect(parts.body).toContain("Install from the marketplace");
    // Nothing is dropped: headline and body still account for every word.
    expect(`${parts.headline} ${parts.body}`.length).toBe(
      PROVIDERS_JOINED.length,
    );
  });

  it("does not read a version number as the end of a sentence", () => {
    expect(splitAnnouncementText(V230).headline).toBe(
      "Bazarr+ v2.3.0 is live!",
    );
  });

  it("keeps a one-sentence announcement as its own headline", () => {
    expect(splitAnnouncementText("New Subtitle Provider!")).toEqual({
      body: "",
      headline: "New Subtitle Provider!",
    });
    expect(splitAnnouncementText("Heads up about a new provider")).toEqual({
      body: "",
      headline: "Heads up about a new provider",
    });
  });

  it("leaves a lead too long to read as a title in the body", () => {
    const runOn = `${"no full stop for a while ".repeat(10)}and then it ends.`;
    const parts = splitAnnouncementText(runOn);

    expect(parts.headline).toBeUndefined();
    expect(parts.body).toBe(runOn);
  });

  it("returns an empty body for empty text", () => {
    expect(splitAnnouncementText("   ")).toEqual({ body: "" });
  });
});

describe("linkHost", () => {
  it("names the host without the www that carries no information", () => {
    expect(linkHost("https://www.example.com/a/b")).toBe("example.com");
    expect(linkHost("https://github.com/LavX/bazarr/releases")).toBe(
      "github.com",
    );
  });

  it("returns nothing for a value that is not an absolute URL", () => {
    expect(linkHost("")).toBeUndefined();
    expect(linkHost("github.com/LavX/bazarr")).toBeUndefined();
  });
});

describe("bodyLabel", () => {
  it("returns a short body whole, on one line", () => {
    expect(bodyLabel("Heads up about\na new provider")).toBe(
      "Heads up about a new provider",
    );
  });

  it("cuts a long body at a word rather than mid word", () => {
    const label = bodyLabel(`${"word ".repeat(30)}end`);

    expect(label.length).toBeLessThanOrEqual(80);
    expect(label.endsWith("word")).toBe(true);
    expect(label).not.toContain("  ");
  });

  it("keeps a first word longer than the limit rather than emptying itself", () => {
    expect(bodyLabel("x".repeat(200))).toBe("x".repeat(80));
  });

  it("returns nothing for an empty body", () => {
    expect(bodyLabel("   ")).toBe("");
  });
});

describe("splitLinks", () => {
  it("lifts a URL out of the sentence around it", () => {
    expect(
      splitLinks(
        "Request it here (https://bazarr.featureupvote.com/suggestions/54294/x). Thanks.",
      ),
    ).toEqual([
      { text: "Request it here (" },
      {
        link: "https://bazarr.featureupvote.com/suggestions/54294/x",
        text: "https://bazarr.featureupvote.com/suggestions/54294/x",
      },
      { text: "). Thanks." },
    ]);
  });

  it("leaves a trailing full stop in the prose", () => {
    expect(splitLinks("See https://lavx.github.io/bazarr.")).toEqual([
      { text: "See " },
      {
        link: "https://lavx.github.io/bazarr",
        text: "https://lavx.github.io/bazarr",
      },
      { text: "." },
    ]);
  });

  it("keeps a bracket the address opened and drops the one that wrapped it", () => {
    expect(splitLinks("(https://en.wikipedia.org/wiki/Foo_(bar))")).toEqual([
      { text: "(" },
      {
        link: "https://en.wikipedia.org/wiki/Foo_(bar)",
        text: "https://en.wikipedia.org/wiki/Foo_(bar)",
      },
      { text: ")" },
    ]);
  });

  it("drops a quote the prose wrapped the address in", () => {
    expect(splitLinks('said "https://example.com/a" once')).toEqual([
      { text: 'said "' },
      {
        link: "https://example.com/a",
        text: "https://example.com/a",
      },
      { text: '" once' },
    ]);
  });

  it("leaves a bare hostname as prose rather than inventing a link", () => {
    expect(
      splitLinks("Catalog: github.com/LavX/bazarr-provider-catalog"),
    ).toEqual([{ text: "Catalog: github.com/LavX/bazarr-provider-catalog" }]);
  });

  it("returns prose untouched when there is no URL at all", () => {
    expect(splitLinks("Python Deprecated!")).toEqual([
      { text: "Python Deprecated!" },
    ]);
    expect(splitLinks("")).toEqual([]);
  });
});
