/**
 * The guards that make a URL which missed the instance's base path fail the
 * run instead of quietly passing against the wrong answer. Under a base
 * path the instance answers such a URL with a redirect to the base, and
 * the supervisor in front of it answers some root paths with a 200 of its
 * own, so a spec that checks ok() would pass against nothing. What is here
 * is used by the fixtures: `guardBrowserContext` on the browser context,
 * which installs the other two in the one call the fixture makes, so the
 * context cannot be handed out with only part of the guard, and
 * `guardApiContext` on the api fixture, so the URL is refused before it
 * is sent.
 */
import type { APIRequestContext } from "@playwright/test";

/**
 * The URL when it is of the instance but outside the base, or null. A URL
 * that is not parseable, such as about:blank, is nobody's and left alone.
 */
export function outsideBase(
  url: string,
  origin: string,
  base: string,
): string | null {
  try {
    const parsed = new URL(url);
    if (
      parsed.origin === origin &&
      parsed.pathname !== base &&
      !parsed.pathname.startsWith(`${base}/`)
    ) {
      return url;
    }
  } catch {
    // Not a parseable URL, such as about:blank: nothing to check.
  }
  return null;
}

/** What the guard needs of a frame. */
export interface GuardedFrame {
  url(): string;
}

/**
 * What the guard needs of a page, which is why a plain stand-in can stand
 * in for one in the harness's checks: the main frame's navigations.
 */
export interface GuardedPage {
  mainFrame(): GuardedFrame;
  on(event: "framenavigated", listener: (frame: GuardedFrame) => void): unknown;
}

/**
 * What the guard needs of a browser context: the requests any of its pages
 * makes, and every page it ever has, including the ones a spec opens itself.
 */
export interface GuardedContext {
  on(event: "request", listener: (request: { url(): string }) => void): unknown;
  on(event: "page", listener: (page: GuardedPage) => void): unknown;
  pages(): GuardedPage[];
}

/**
 * Watches the context for anything that reaches the instance outside the
 * base: every request, whichever page made it, and the main-frame
 * navigations of every page, including a page opened after this was called
 * or a popup. Returns the list of what was reached, each URL once, for the
 * fixture to fail the test with once the test is done.
 */
export function watchContext(
  context: GuardedContext,
  baseURL: string,
  base: string,
): () => string[] {
  const origin = new URL(baseURL).origin;
  const reached: string[] = [];
  const watchPage = (page: GuardedPage): void => {
    page.on("framenavigated", (frame) => {
      if (frame !== page.mainFrame()) return;
      const url = outsideBase(frame.url(), origin, base);
      if (url !== null) reached.push(`navigation ${url}`);
    });
  };
  context.on("request", (request) => {
    const url = outsideBase(request.url(), origin, base);
    if (url !== null) reached.push(`request ${url}`);
  });
  // The page the fixtures hand out is opened after this, so the page event
  // catches it; a page that already exists is watched directly.
  context.on("page", watchPage);
  for (const page of context.pages()) watchPage(page);
  return () => [...new Set(reached)];
}

/** The api fixture's methods that send a request. */
const SENDING = ["get", "post", "put", "patch", "delete", "head", "fetch"];

/**
 * A guarded request context, under a base path: a URL that resolves
 * outside the base, however it is written, is refused before anything is
 * sent. It is what the api fixture hands out, and what the browser
 * context's own request property is replaced with, so the pages the
 * context creates inherit the same refusal. At the root of a host the
 * context is handed back unchanged.
 */
export function guardApiContext(
  context: APIRequestContext,
  baseURL: string,
  base: string,
): APIRequestContext {
  if (base === "") return context;
  const origin = new URL(baseURL).origin;
  const send = (method: string, url: unknown, rest: unknown[]) => {
    if (typeof url !== "string") {
      throw new Error(
        "the api fixture sends a URL given as a string, built with appPath()",
      );
    }
    // The same resolution Playwright itself applies, so what is refused is
    // what would have been sent.
    const resolved = new URL(url, baseURL);
    if (outsideBase(resolved.href, origin, base) !== null) {
      throw new Error(
        `the instance is served under ${base}: ${JSON.stringify(url)} resolves to ${resolved.href}, which is outside the base. Build the URL with appPath().`,
      );
    }
    const anyContext = context as unknown as Record<
      string,
      (...args: unknown[]) => unknown
    >;
    return anyContext[method](url, ...rest);
  };
  return new Proxy(context, {
    get(target, property, receiver) {
      if (typeof property === "string" && SENDING.includes(property)) {
        return (url: unknown, ...rest: unknown[]) => send(property, url, rest);
      }
      const value = Reflect.get(target, property, receiver);
      return typeof value === "function"
        ? (value as (...args: unknown[]) => unknown).bind(target)
        : value;
    },
  });
}

/**
 * What the guard needs of the browser context whose request property is
 * replaced: the request context itself, which is all it reads and swaps.
 */
export interface ContextWithRequest {
  request: APIRequestContext;
}

/**
 * Replaces the browser context's request property with the guarded api
 * context, before the fixtures hand the context to a spec. Playwright
 * copies the context's request property into every page it creates, so a
 * page opened under any name, or the context itself, refuses a URL that
 * resolves outside the base before it is sent, instead of following the
 * redirect the instance answers it with. The parameter is named for what
 * it is, not `context`, whose `.request` read is the very shortcut the
 * lint rule flags in a spec.
 */
export function guardContextRequest(
  browserContext: ContextWithRequest,
  baseURL: string,
  base: string,
): void {
  Object.defineProperty(browserContext, "request", {
    value: guardApiContext(browserContext.request, baseURL, base),
  });
}

/**
 * The one guard call the context fixture makes, so the guard cannot be
 * installed in part: the watch on the browser context and the guarded
 * request property in the same call, and the list of what reached the
 * instance outside the base, for the fixture to fail the test with once
 * the test is done. A fixture that drops the call hands out a context
 * with neither layer, so the call is the one thing the fixture does
 * under a base.
 */
export function guardBrowserContext(
  browserContext: GuardedContext & ContextWithRequest,
  baseURL: string,
  base: string,
): () => string[] {
  const violations = watchContext(browserContext, baseURL, base);
  guardContextRequest(browserContext, baseURL, base);
  return violations;
}
