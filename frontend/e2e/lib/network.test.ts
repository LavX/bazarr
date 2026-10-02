/**
 * The page wrapper that loads a page again when Chromium dropped part of it
 * for a host network change. A stand-in page plays back what each load
 * lost, so the checks need no browser.
 */
import type { Page } from "@playwright/test";
import { describe, expect, it, vi } from "vitest";
import { NETWORK_CHANGED, reloadOnNetworkChange } from "./network";

interface Dropped {
  type: string;
  error: string;
  /** The frame the request belongs to; "none" for a service worker's. */
  frame?: "main" | "child" | "none";
}

interface Load {
  dropped?: Dropped[];
  throws?: string;
}

/** A page whose nth load drops what `loads[n]` says; the last one repeats. */
function fakePage(loads: Load[]) {
  const main = { name: "main" };
  const child = { name: "child" };
  const listeners: ((request: unknown) => void)[] = [];
  let count = 0;
  const load = async () => {
    const step = loads[Math.min(count, loads.length - 1)];
    count += 1;
    for (const dropped of step.dropped ?? []) {
      const request = {
        failure: () => ({ errorText: dropped.error }),
        resourceType: () => dropped.type,
        frame: () => {
          if (dropped.frame === "none") {
            throw new Error("Service Worker requests do not have a frame");
          }
          return dropped.frame === "child" ? child : main;
        },
      };
      for (const listener of listeners) listener(request);
    }
    if (step.throws) throw new Error(`page.goto: ${step.throws}`);
    return { load: count };
  };
  const goto = vi.fn(load);
  const reload = vi.fn(load);
  const page = {
    on(event: string, listener: (request: unknown) => void) {
      if (event === "requestfailed") listeners.push(listener);
      return page;
    },
    mainFrame: () => main,
    goto,
    reload,
  };
  return { page: page as unknown as Page, goto, reload };
}

const script = (error = NETWORK_CHANGED): Dropped => ({
  type: "script",
  error,
});

function wrapped(loads: Load[]) {
  const fake = fakePage(loads);
  const annotations: { type: string; description?: string }[] = [];
  reloadOnNetworkChange(fake.page, { annotations });
  return { ...fake, annotations };
}

describe("loading a page again after a host network change", () => {
  it("makes a navigation again when the change dropped its scripts", async () => {
    const { page, goto, annotations } = wrapped([
      { dropped: [script(), { type: "stylesheet", error: NETWORK_CHANGED }] },
      {},
    ]);

    const response = await page.goto("/system/status");

    expect(goto).toHaveBeenCalledTimes(2);
    expect(goto).toHaveBeenLastCalledWith("/system/status", undefined);
    expect(response).toEqual({ load: 2 });
    expect(annotations).toEqual([
      expect.objectContaining({
        type: "network changed",
        description: expect.stringContaining("/system/status"),
      }),
    ]);
  });

  it("does the same for a reload", async () => {
    const { page, reload } = wrapped([{ dropped: [script()] }, {}]);

    await page.reload();

    expect(reload).toHaveBeenCalledTimes(2);
  });

  it("makes a navigation again when the change failed the document itself", async () => {
    const { page, goto } = wrapped([{ throws: NETWORK_CHANGED }, {}]);

    await expect(page.goto("/")).resolves.toEqual({ load: 2 });
    expect(goto).toHaveBeenCalledTimes(2);
  });

  it("leaves every other failure for the test to see", async () => {
    const { page, goto, annotations } = wrapped([
      {
        dropped: [
          script("net::ERR_CONNECTION_REFUSED"),
          { type: "image", error: NETWORK_CHANGED },
          { type: "script", error: NETWORK_CHANGED, frame: "child" },
          { type: "fetch", error: NETWORK_CHANGED, frame: "none" },
        ],
      },
    ]);

    await page.goto("/");

    expect(goto).toHaveBeenCalledTimes(1);
    expect(annotations).toEqual([]);

    const refused = wrapped([{ throws: "net::ERR_CONNECTION_REFUSED" }]);
    await expect(refused.page.goto("/")).rejects.toThrow(/REFUSED/);
    expect(refused.goto).toHaveBeenCalledTimes(1);
  });

  it("stops after two more tries, so a network that keeps changing still fails the test", async () => {
    const dropping = wrapped([{ dropped: [script()] }]);
    await dropping.page.goto("/");
    expect(dropping.goto).toHaveBeenCalledTimes(3);

    const failing = wrapped([{ throws: NETWORK_CHANGED }]);
    await expect(failing.page.goto("/")).rejects.toThrow(NETWORK_CHANGED);
    expect(failing.goto).toHaveBeenCalledTimes(3);
  });
});
