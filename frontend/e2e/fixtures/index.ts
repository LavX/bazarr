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
 * a request context that sends the API key on every call. Against an instance
 * with a login, every browser context starts with the session global setup
 * signed in once for the run. `page` loads a page again when a host network
 * change dropped its scripts (see lib/network.ts).
 */
import type { BazarrInstance, ContainerSlot } from "@e2e/lib/container";
import {
  containerForFile,
  DEFAULT_IMAGE,
  dockerUnavailable,
  externalBazarr,
  REMOVAL_ALLOWANCE_MS,
} from "@e2e/lib/container";
import { reloadOnNetworkChange } from "@e2e/lib/network";
import { sharedBazarr } from "@e2e/lib/shared";
import type { APIRequestContext } from "@playwright/test";
import { expect, test as base } from "@playwright/test";

interface WorkerFixtures {
  containerSlot: ContainerSlot;
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

  page: async ({ page }, use, testInfo) => {
    reloadOnNetworkChange(page, testInfo);
    await use(page);
  },

  storageState: async ({ storageState }, use) => {
    await use(process.env.BAZARR_E2E_STORAGE_STATE ?? storageState);
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
