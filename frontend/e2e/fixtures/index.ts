/**
 * The test object every spec imports.
 *
 *   import { expect, test } from "@e2e/fixtures";
 *
 * `bazarr` is the instance under test. What it is depends on the project:
 *
 * - stateful: a fresh container and volume per spec file. It is held by the
 *   worker and replaced when the worker moves on to the next file, so tests in
 *   one file share it and files never do. A failed test restarts the worker,
 *   which throws the container away with it.
 * - stateless: one onboarded container shared by every stateless spec in the
 *   run, started by whichever worker needs it first.
 *
 * With BAZARR_E2E_URL set, both use that instance and no docker is involved.
 *
 * `baseURL` follows `bazarr`, and every page and API path a spec builds goes
 * through `appPath()` from `@e2e/lib/base`, so the same spec runs at the root
 * of a host and under the sub-path the instance is served under. Under a base
 * path the fixtures guard a URL the spec itself wrote that missed the base:
 * `api` refuses, before sending, a URL that resolves outside the base, and
 * Playwright's own `request` fixture is refused in its favour; the context
 * gets the whole guard in one call (`guardBrowserContext`), whose watch
 * covers every page the context ever has, including one a spec opens itself
 * and popups, and fails the test in teardown with every request and
 * main-frame navigation that reached the instance outside the base, and
 * whose guarded request property every page the context creates inherits,
 * so it refuses such a URL before it is sent. The direct forms of creating
 * a context, a page or a browser outside the fixtures are what the lint
 * rule rejects, and under a base the request factory refuses the context a
 * spec asks it for; a browser or a context created some other way is the
 * one thing the run does not watch. `api` is
 * a request context that sends the API key on every call. Against an
 * instance with a login, every browser context starts with the session
 * global setup signed in once for the run. `page` loads a page again when a
 * host network change dropped its scripts (see lib/network.ts).
 */
import { basePath } from "@e2e/lib/base";
import type { BazarrInstance, ContainerSlot } from "@e2e/lib/container";
import {
  containerForFile,
  DEFAULT_IMAGE,
  dockerUnavailable,
  externalBazarr,
  REMOVAL_ALLOWANCE_MS,
} from "@e2e/lib/container";
import { guardApiContext, guardBrowserContext } from "@e2e/lib/guard";
import { reloadOnNetworkChange } from "@e2e/lib/network";
import { sharedBazarr } from "@e2e/lib/shared";
import type { APIRequest, APIRequestContext } from "@playwright/test";
import { expect, test as base } from "@playwright/test";

/** The options of the request factory's newContext, as the api fixture passes. */
type RequestFactoryOptions = Parameters<APIRequest["newContext"]>[0];

interface WorkerFixtures {
  containerSlot: ContainerSlot;
  /**
   * The wrap that refuses a request context the factory is asked for
   * under a base path, installed for the whole worker and removed when
   * it is done. It publishes nothing, and its void type is the check
   * that keeps it that way: a spec that names the fixture gets a type
   * no call goes through. The api fixture creates its own context
   * through the factory's prototype, which the wrap does not shadow.
   */
  newRequestContext: void;
}

interface TestFixtures {
  bazarr: BazarrInstance;
  api: APIRequestContext;
}

export const test = base.extend<TestFixtures, WorkerFixtures>({
  containerSlot: [
    async ({}, use) => {
      const slot: ContainerSlot = { held: null };
      await use(slot);
      await slot.held?.instance.stop();
    },
    // A worker fixture otherwise gets the suite's test timeout, and removing
    // the last file's container can take longer than that on a busy host.
    { scope: "worker", timeout: REMOVAL_ALLOWANCE_MS },
  ],

  newRequestContext: [
    async ({ playwright }, use) => {
      if (basePath() === "") {
        await use();
        return;
      }
      // The playwright fixture's playwright.request and the request export
      // of @playwright/test are the one object, so one wrap covers a
      // context a spec asks of either name. A context made outside the
      // fixtures reaches the instance without the API key and unseen by
      // the watch, so under a base it is refused. It is an auto fixture,
      // so every test runs with the wrap, even one that uses no fixture
      // at all, and it hands the spec nothing: the api fixture reaches
      // the factory's own newContext through the prototype, which the
      // wrap only shadows.
      const factory = playwright.request;
      factory.newContext = () => {
        throw new Error(
          "under a base path a request context created outside the fixtures reaches the instance without the API key and unseen by the watch: use the api fixture, which sends the key and refuses a URL that resolves outside the base before sending it",
        );
      };
      await use();
      // newContext lives on the factory's prototype, so the shadowing
      // property comes off with a delete.
      Reflect.deleteProperty(factory, "newContext");
    },
    { scope: "worker", auto: true },
  ],

  bazarr: async ({ containerSlot }, use, testInfo) => {
    const external = process.env.BAZARR_E2E_URL;
    if (external) {
      await use(await externalBazarr(external));
      return;
    }
    const image = process.env.BAZARR_E2E_IMAGE ?? DEFAULT_IMAGE;
    const missing = await dockerUnavailable(image);
    testInfo.skip(missing !== null, `Skipped: ${missing}`);

    if (testInfo.project.name === "stateless") {
      await use(await sharedBazarr(testInfo));
      return;
    }

    await use(
      await containerForFile(containerSlot, testInfo.file, testInfo, {
        image,
      }),
    );
  },

  context: async ({ context, bazarr }, use) => {
    const base = basePath();
    if (base === "") {
      await use(context);
      return;
    }
    // A root-relative URL resolves against the host, and the instance
    // answers it with a redirect to the base, so the wrong page loads and
    // looks fine: the false pass this guard exists for. Whatever wrote the
    // URL (a variable, a helper argument, an end-anchored regex), the
    // request or navigation itself still goes outside the base. One call
    // installs both layers, so the context cannot be handed out with only
    // one of them: the watch is on the context, so it covers every page
    // of the context, including one a spec opens itself and popups, and
    // fails the test once it is done, with the list; and the context's own
    // request property is replaced with the guarded one before the
    // context is handed out, so a page created under any name inherits a
    // request that refuses such a URL before it is sent: a request
    // context fires no page request events, so the watch would see none
    // of it.
    const violations = guardBrowserContext(context, bazarr.baseURL, base);
    await use(context);
    const reached = violations();
    if (reached.length > 0) {
      throw new Error(
        `the instance is served under ${base}, but the test reached it outside the base:\n${reached.join("\n")}`,
      );
    }
  },

  page: async ({ page }, use, testInfo) => {
    reloadOnNetworkChange(page, testInfo);
    await use(page);
  },

  request: async ({ request }, use) => {
    if (basePath() !== "") {
      throw new Error(
        "under a base path the request fixture is not guarded against a missed base: use the api fixture, which refuses a URL that resolves outside the base before sending it",
      );
    }
    await use(request);
  },

  storageState: async ({ storageState }, use) => {
    await use(process.env.BAZARR_E2E_STORAGE_STATE ?? storageState);
  },

  baseURL: async ({ bazarr }, use) => {
    await use(bazarr.baseURL);
  },

  api: async ({ bazarr, playwright }, use) => {
    // The wrap the worker fixture installs only shadows the instance, so
    // the method on the factory's prototype is the original, the one
    // nothing rewraps: the context the api fixture builds its guard on
    // is the factory's own.
    const options: RequestFactoryOptions = {
      baseURL: bazarr.baseURL,
      extraHTTPHeaders: { "X-API-KEY": bazarr.apiKey },
      // The instance answers a URL that missed its base with a redirect to
      // the base, and the supervisor answers some root paths with a 200
      // of its own, so a spec that checks ok() could pass against nothing.
      // The guard refuses such a URL before it is sent; the redirect
      // refusal is the backstop for whichever answer still slips through.
      ...(basePath() === "" ? {} : { maxRedirects: 0 }),
    };
    const context = await Object.getPrototypeOf(
      playwright.request,
    ).newContext.call(playwright.request, options);
    await use(guardApiContext(context, bazarr.baseURL, basePath()));
    await context.dispose();
  },
});

export { expect };
