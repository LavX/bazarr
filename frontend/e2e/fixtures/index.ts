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
 * `baseURL` follows `bazarr`, so page.goto("/setup") just works, and `api` is
 * a request context that sends the API key on every call.
 */
import type { BazarrInstance } from "@e2e/lib/container";
import {
  DEFAULT_IMAGE,
  dockerUnavailable,
  externalBazarr,
  startBazarr,
} from "@e2e/lib/container";
import { sharedBazarr } from "@e2e/lib/shared";
import type { APIRequestContext } from "@playwright/test";
import { expect, test as base } from "@playwright/test";

/** Extra time a test gets when it has to wait for a container to boot. */
const STARTUP_ALLOWANCE_MS = 200_000;

interface Held {
  file: string;
  instance: BazarrInstance;
}

interface WorkerFixtures {
  containerSlot: { held: Held | null };
}

interface TestFixtures {
  bazarr: BazarrInstance;
  api: APIRequestContext;
}

export const test = base.extend<TestFixtures, WorkerFixtures>({
  containerSlot: [
    async ({}, use) => {
      const slot: { held: Held | null } = { held: null };
      await use(slot);
      await slot.held?.instance.stop();
    },
    { scope: "worker" },
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
      testInfo.setTimeout(testInfo.timeout + STARTUP_ALLOWANCE_MS);
      await use(await sharedBazarr());
      return;
    }

    if (containerSlot.held?.file !== testInfo.file) {
      await containerSlot.held?.instance.stop();
      containerSlot.held = null;
      testInfo.setTimeout(testInfo.timeout + STARTUP_ALLOWANCE_MS);
      containerSlot.held = {
        file: testInfo.file,
        instance: await startBazarr({ image }),
      };
    }
    await use(containerSlot.held.instance);
  },

  baseURL: async ({ bazarr }, use) => {
    await use(bazarr.baseURL);
  },

  api: async ({ playwright, bazarr }, use) => {
    const context = await playwright.request.newContext({
      baseURL: bazarr.baseURL,
      extraHTTPHeaders: { "X-API-KEY": bazarr.apiKey },
    });
    await use(context);
    await context.dispose();
  },
});

export { expect };
