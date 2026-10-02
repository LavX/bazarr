/**
 * The end-to-end suite marks the release tour seen before every spec that is
 * about something else. What it writes has to be exactly what this build
 * reads, or the release that moves the tour version opens the modal over
 * every one of those specs and fails them all.
 */
import type { Page } from "@playwright/test";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getWhatsNewSlides, latestWhatsNewVersion } from "@/data/whatsNew";
import {
  getSeenWhatsNewVersion,
  shouldAutoOpenWhatsNew,
} from "@/utilities/whatsNew";

type SuiteModule = typeof import("../../../e2e/lib/whatsNew");

// The suite lives outside src, where the app's alias does not reach.
const loadSuiteModule = (): Promise<SuiteModule> =>
  import("../../../e2e/lib/whatsNew");

/** A page that runs an init script at once, as a browser does on load. */
function loadingPage(): Page {
  return {
    addInitScript: async (script: (arg: unknown) => void, arg: unknown) =>
      script(arg),
  } as unknown as Page;
}

describe("the end-to-end What's New marker", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.resetModules();
  });

  afterEach(() => {
    vi.doUnmock("@/data/whatsNew");
    vi.doUnmock("@/utilities/whatsNew");
    vi.resetModules();
  });

  it("marks the release this build announces as seen, under the key it reads", async () => {
    const suite = await loadSuiteModule();

    await suite.skipWhatsNew(loadingPage());

    expect(suite.WHATS_NEW_VERSION).toBe(latestWhatsNewVersion);
    expect(
      shouldAutoOpenWhatsNew(
        getSeenWhatsNewVersion(),
        latestWhatsNewVersion,
        getWhatsNewSlides(latestWhatsNewVersion).length,
      ),
    ).toBe(false);
  });

  it("follows the app when a release moves the version or the key", async () => {
    vi.doMock("@/data/whatsNew", () => ({ latestWhatsNewVersion: "99.1.0" }));
    vi.doMock("@/utilities/whatsNew", () => ({
      WHATS_NEW_SEEN_KEY: "bazarr-whats-new-seen-next",
    }));
    const suite = await loadSuiteModule();

    await suite.skipWhatsNew(loadingPage());

    expect(suite.WHATS_NEW_VERSION).toBe("99.1.0");
    expect(suite.WHATS_NEW_SEEN_KEY).toBe("bazarr-whats-new-seen-next");
    expect(localStorage.getItem("bazarr-whats-new-seen-next")).toBe("99.1.0");
  });
});
