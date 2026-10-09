/**
 * Whenever an interface on the host gains or loses an address, Chromium fails
 * every request still waiting for a connection, to 127.0.0.1 as much as
 * anywhere, with net::ERR_NETWORK_CHANGED. Docker does that each time a
 * container starts or stops, whether this suite's or any other on the
 * machine, because the new interface gets an IPv6 link-local address. The
 * app's first load asks for more scripts and styles than Chromium opens
 * connections to one host, so some of them are always waiting, and a page
 * that loses them stays blank for good.
 *
 * So a navigation a test makes is made again, up to twice, when the page it
 * loaded lost part of itself like that, and the test is annotated to say so.
 * Every other failure is left for the test to see.
 */
import type { Page, Request, Response, TestInfo } from "@playwright/test";

export const NETWORK_CHANGED = "net::ERR_NETWORK_CHANGED";

/** How many times a navigation is made again. */
const RETRIES = 2;
/** What a page cannot come up without. */
const PAGE_PARTS = new Set(["document", "script", "stylesheet"]);

function inMainFrame(page: Page, request: Request): boolean {
  try {
    return request.frame() === page.mainFrame();
  } catch {
    // A service worker's requests have no frame.
    return false;
  }
}

export function reloadOnNetworkChange(
  page: Page,
  testInfo: Pick<TestInfo, "annotations">,
): void {
  let dropped = 0;
  page.on("requestfailed", (request) => {
    if (
      request.failure()?.errorText === NETWORK_CHANGED &&
      PAGE_PARTS.has(request.resourceType()) &&
      inMainFrame(page, request)
    ) {
      dropped += 1;
    }
  });

  const again = (what: string, why: string) =>
    testInfo.annotations.push({
      type: "network changed",
      description: `${what} was loaded again: ${why} by a host network change (${NETWORK_CHANGED})`,
    });

  const load = async (
    what: string,
    navigate: () => Promise<Response | null>,
  ): Promise<Response | null> => {
    for (let attempt = 0; ; attempt += 1) {
      dropped = 0;
      let response: Response | null;
      try {
        response = await navigate();
      } catch (error) {
        if (attempt === RETRIES || !String(error).includes(NETWORK_CHANGED)) {
          throw error;
        }
        again(what, "the page itself was dropped");
        continue;
      }
      if (dropped === 0 || attempt === RETRIES) return response;
      again(what, `${dropped} of its scripts and styles were dropped`);
    }
  };

  const goto = page.goto.bind(page);
  const reload = page.reload.bind(page);
  page.goto = (url, options) => load(url, () => goto(url, options));
  page.reload = (options) => load("the page", () => reload(options));
}
