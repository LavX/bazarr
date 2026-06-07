import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  getSeenWhatsNewVersion,
  markWhatsNewSeen,
  navigateApp,
  registerAppNavigate,
  shouldAutoOpenWhatsNew,
  WHATS_NEW_SEEN_KEY,
} from "@/utilities/whatsNew";

describe("whats-new auto-open logic", () => {
  beforeEach(() => localStorage.clear());

  it("shows when no token is stored (no-token -> show)", () => {
    expect(shouldAutoOpenWhatsNew(null, "2.4.0", 3)).toBe(true);
  });

  it("does not show when the seen token equals the latest version", () => {
    expect(shouldAutoOpenWhatsNew("2.4.0", "2.4.0", 3)).toBe(false);
  });

  it("shows when the seen token differs from the latest version", () => {
    expect(shouldAutoOpenWhatsNew("2.3.0", "2.4.0", 3)).toBe(true);
  });

  it("does not show when there are no slides for the latest version", () => {
    expect(shouldAutoOpenWhatsNew(null, "2.4.0", 0)).toBe(false);
  });

  it("persists and reads the seen version", () => {
    expect(getSeenWhatsNewVersion()).toBeNull();
    markWhatsNewSeen("2.4.0");
    expect(getSeenWhatsNewVersion()).toBe("2.4.0");
    expect(localStorage.getItem(WHATS_NEW_SEEN_KEY)).toBe("2.4.0");
  });
});

describe("app navigate bridge", () => {
  it("routes navigateApp through the registered navigate function", () => {
    const nav = vi.fn();
    registerAppNavigate(nav);
    navigateApp("/distribution-hub");
    expect(nav).toHaveBeenCalledWith("/distribution-hub");
  });

  it("is a no-op once unregistered", () => {
    const nav = vi.fn();
    registerAppNavigate(nav);
    registerAppNavigate(null);
    navigateApp("/series");
    expect(nav).not.toHaveBeenCalled();
  });
});
