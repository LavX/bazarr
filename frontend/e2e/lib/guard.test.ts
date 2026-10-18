/**
 * The sub-path guard of the fixtures, checked in plain Node: the pages and
 * the request context are plain stand-ins, because what is under test is
 * which URLs the guard collects, from every page the context ever has, and
 * which it refuses before sending.
 */
import type {
  ContextWithRequest,
  GuardedContext,
  GuardedFrame,
  GuardedPage,
} from "@e2e/lib/guard";
import {
  guardApiContext,
  guardBrowserContext,
  guardContextRequest,
  watchContext,
} from "@e2e/lib/guard";
import type { APIRequestContext } from "@playwright/test";
import { describe, expect, it } from "vitest";

const ORIGIN = "http://127.0.0.1:6790";
const UNDER = `${ORIGIN}/bazarr`;

/** A stand-in page: only its main frame's navigations are watched. */
class FakePage implements GuardedPage {
  private url: string;
  private readonly listeners: Array<(frame: GuardedFrame) => void> = [];
  readonly main: GuardedFrame;

  constructor(url: string) {
    this.url = url;
    this.main = { url: () => this.url };
  }

  mainFrame(): GuardedFrame {
    return this.main;
  }

  on(
    _event: "framenavigated",
    listener: (frame: GuardedFrame) => void,
  ): unknown {
    this.listeners.push(listener);
    return this;
  }

  /** Navigates the main frame, as a goto or a click's navigation would. */
  navigate(url: string): void {
    this.url = url;
    for (const listener of this.listeners) listener(this.main);
  }
}

/** A stand-in context: the pages it opens fire its page event. */
class FakeContext implements GuardedContext {
  readonly created: FakePage[] = [];
  private readonly requests: Array<(request: { url(): string }) => void> = [];
  private readonly pageListeners: Array<(page: GuardedPage) => void> = [];

  on(event: "request", listener: (request: { url(): string }) => void): unknown;
  on(event: "page", listener: (page: GuardedPage) => void): unknown;
  on(event: "request" | "page", listener: (argument: never) => void): unknown {
    if (event === "request") {
      this.requests.push(listener as (request: { url(): string }) => void);
    } else {
      this.pageListeners.push(listener as (page: GuardedPage) => void);
    }
    return this;
  }

  pages(): GuardedPage[] {
    return this.created;
  }

  /** A page opened after the guard attached, as newPage() and popups are. */
  openPage(url: string): FakePage {
    const page = new FakePage(url);
    this.created.push(page);
    for (const listener of this.pageListeners) listener(page);
    return page;
  }

  /** A request the context made, whichever of its pages made it. */
  request(url: string): void {
    for (const listener of this.requests) listener({ url: () => url });
  }
}

/** A stand-in request context: it records what it was allowed to send. */
function fakeApiContext() {
  const sent: string[] = [];
  const send =
    (method: string) =>
    async (url: string): Promise<null> => {
      sent.push(`${method} ${url}`);
      return null;
    };
  const context = {
    get: send("get"),
    post: send("post"),
    put: send("put"),
    patch: send("patch"),
    delete: send("delete"),
    head: send("head"),
    fetch: send("fetch"),
    dispose: async () => undefined,
  } as unknown as APIRequestContext;
  return { context, sent };
}

/**
 * A stand-in browser context: a page copies its request property when it
 * is created, as Playwright's pages do, so a page opened after the guard
 * replaced that property inherits the guarded request. It carries the
 * context's events and the pages it opened too, which is everything the
 * guards install themselves on.
 */
class FakeBrowserContext implements GuardedContext, ContextWithRequest {
  readonly created: Array<FakePage & { request: APIRequestContext }> = [];
  private readonly requests: Array<(request: { url(): string }) => void> = [];
  private readonly pageListeners: Array<(page: GuardedPage) => void> = [];
  request: APIRequestContext;

  constructor(request: APIRequestContext) {
    this.request = request;
  }

  on(event: "request", listener: (request: { url(): string }) => void): unknown;
  on(event: "page", listener: (page: GuardedPage) => void): unknown;
  on(event: "request" | "page", listener: (argument: never) => void): unknown {
    if (event === "request") {
      this.requests.push(listener as (request: { url(): string }) => void);
    } else {
      this.pageListeners.push(listener as (page: GuardedPage) => void);
    }
    return this;
  }

  pages(): GuardedPage[] {
    return this.created;
  }

  /** A page the context created, carrying the request property it copied. */
  newPage(url = "about:blank"): FakePage & { request: APIRequestContext } {
    const page = Object.assign(new FakePage(url), { request: this.request });
    this.created.push(page);
    for (const listener of this.pageListeners) listener(page);
    return page;
  }

  /** A request the context made, whichever of its pages made it. */
  madeRequest(url: string): void {
    for (const listener of this.requests) listener({ url: () => url });
  }
}

describe("the context-level guard of a sub-path run", () => {
  it("collects the requests and navigations of the page the fixture handed out", () => {
    const context = new FakeContext();
    const page = context.openPage(UNDER);
    const violations = watchContext(context, UNDER, "/bazarr");

    // Inside the base, either way it is written: nothing to report.
    context.request(`${UNDER}/api/system/settings`);
    page.navigate(`${UNDER}/settings`);
    expect(violations()).toEqual([]);

    // Outside it: collected, however the URL was written.
    context.request(`${ORIGIN}/api/system/settings`);
    page.navigate(`${ORIGIN}/settings`);
    expect(violations()).toEqual([
      `request ${ORIGIN}/api/system/settings`,
      `navigation ${ORIGIN}/settings`,
    ]);
  });

  it("collects the same from a page the spec opens itself", () => {
    const context = new FakeContext();
    const violations = watchContext(context, UNDER, "/bazarr");
    const second = context.openPage("about:blank");
    second.navigate(`${ORIGIN}/settings`);
    expect(violations()).toEqual([`navigation ${ORIGIN}/settings`]);
  });

  it("collects the same from a popup, and without repeating itself", () => {
    const context = new FakeContext();
    const violations = watchContext(context, UNDER, "/bazarr");
    const popup = context.openPage("about:blank");
    popup.navigate(`${ORIGIN}/history`);
    popup.navigate(`${ORIGIN}/history`);
    context.request(`${ORIGIN}/history`);
    // The navigation and the request are both reported, each once.
    expect(violations()).toEqual([
      `navigation ${ORIGIN}/history`,
      `request ${ORIGIN}/history`,
    ]);
  });

  it("watches a page that already existed when the guard attached", () => {
    const context = new FakeContext();
    context.openPage(UNDER);
    const violations = watchContext(context, UNDER, "/bazarr");
    context.created[0].navigate(`${ORIGIN}/settings`);
    expect(violations()).toEqual([`navigation ${ORIGIN}/settings`]);
  });

  it("reports each URL once, however many times it was reached", () => {
    const context = new FakeContext();
    const violations = watchContext(context, UNDER, "/bazarr");
    context.request(`${ORIGIN}/api/system/settings`);
    context.request(`${ORIGIN}/api/system/settings`);
    expect(violations()).toEqual([`request ${ORIGIN}/api/system/settings`]);
  });

  it("leaves alone a URL that is not of the instance", () => {
    const context = new FakeContext();
    const violations = watchContext(context, UNDER, "/bazarr");
    const page = context.openPage("about:blank");
    page.navigate("about:blank");
    context.request("data:text/plain,nothing");
    expect(violations()).toEqual([]);
  });
});

describe("the api fixture's guard", () => {
  it("refuses a root-relative URL before sending it", () => {
    const { context, sent } = fakeApiContext();
    const api = guardApiContext(context, UNDER, "/bazarr");

    expect(() => api.get("/api/system/settings")).toThrow(/outside the base/);
    expect(sent).toEqual([]);
  });

  it("refuses an absolute URL outside the base, and the paths the supervisor answers at the root", () => {
    const { context, sent } = fakeApiContext();
    const api = guardApiContext(context, UNDER, "/bazarr");

    expect(() => api.get(`${ORIGIN}/api/system/settings`)).toThrow(
      /outside the base/,
    );
    // The supervisor answers some root paths with a 200 of its own, so a
    // plain redirect refusal would let this one through.
    expect(() => api.get("/_supervisor/status")).toThrow(/outside the base/);
    expect(() => api.post("/settings", {})).toThrow(/outside the base/);
    expect(() => api.fetch("/api/system/status")).toThrow(/outside the base/);
    expect(sent).toEqual([]);
  });

  it("sends a URL under the base, however it is written", async () => {
    const { context, sent } = fakeApiContext();
    const api = guardApiContext(context, UNDER, "/bazarr");

    await api.get("/bazarr/api/system/settings");
    await api.get(`${UNDER}/api/system/settings`);
    expect(sent).toEqual([
      `get /bazarr/api/system/settings`,
      `get ${UNDER}/api/system/settings`,
    ]);
  });

  it("passes the context through unchanged at the root of a host", async () => {
    const { context, sent } = fakeApiContext();
    const api = guardApiContext(context, ORIGIN, "");

    expect(api).toBe(context);
    await api.get("/api/system/settings");
    expect(sent).toEqual(["get /api/system/settings"]);
  });
});

describe("the request property of the context itself", () => {
  it("is the guarded one, and a page created under any other name inherits it", () => {
    const { context, sent } = fakeApiContext();
    const browserContext = new FakeBrowserContext(context);
    guardContextRequest(browserContext, UNDER, "/bazarr");

    // A page the spec opens itself, held under a name the lint rule does
    // not know, and the URL in a variable: both are still refused before
    // anything is sent.
    const second = browserContext.newPage();
    const path = "/_supervisor/status";
    expect(() => browserContext.request.get(path)).toThrow(/outside the base/);
    expect(() => second.request.get(path)).toThrow(/outside the base/);
    expect(sent).toEqual([]);
  });
});

describe("the one call that guards the browser context", () => {
  it("installs the watch and the guarded request property together", () => {
    const { context, sent } = fakeApiContext();
    const browserContext = new FakeBrowserContext(context);

    const violations = guardBrowserContext(browserContext, UNDER, "/bazarr");

    // The request property is the guarded one, and a page the context
    // creates after the call inherits it: both refuse before sending.
    const second = browserContext.newPage();
    const path = "/_supervisor/status";
    expect(() => browserContext.request.get(path)).toThrow(/outside the base/);
    expect(() => second.request.get(path)).toThrow(/outside the base/);
    expect(sent).toEqual([]);

    // The watch is on the context: a request it made and the main-frame
    // navigation of a page it opened are both collected, for the fixture
    // to fail the test with once the test is done.
    browserContext.madeRequest(`${ORIGIN}/api/system/settings`);
    second.navigate(`${ORIGIN}/settings`);
    expect(violations()).toEqual([
      `request ${ORIGIN}/api/system/settings`,
      `navigation ${ORIGIN}/settings`,
    ]);
  });
});
