import type { FullConfig } from "@playwright/test";
import { chromium } from "@playwright/test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { credentials, loginIfAsked } from "./lib/auth";
import { basePath } from "./lib/base";
import { DEFAULT_IMAGE, externalBaseURL, imageId } from "./lib/container";

/**
 * Gives the run a private scratch directory the workers can coordinate in.
 *
 * Against an existing instance with a login, it also signs in once for the
 * whole run and leaves the session where the fixtures pick it up, so every
 * browser context starts logged in and the form is only ever filled once.
 */
export default async function globalSetup(config: FullConfig) {
  // Fails the run before anything is started when the base path is not a
  // usable one, or when it disagrees with the path of BAZARR_E2E_URL.
  basePath();
  const runDir = await mkdtemp(join(tmpdir(), "bazarr-e2e-"));
  process.env.BAZARR_E2E_RUN_DIR = runDir;

  // The report's metadata records what the run answered to: the image tag
  // and the docker id of that image, and the base path the instance was
  // served under, so a report of a release pair says which image it ran
  // against and which of the two runs it is. An image that cannot be
  // inspected is recorded without its id, and an external BAZARR_E2E_URL
  // runs no image at all.
  config.metadata.bazarrBasePath = basePath();
  if (!process.env.BAZARR_E2E_URL) {
    const image = process.env.BAZARR_E2E_IMAGE ?? DEFAULT_IMAGE;
    config.metadata.bazarrImage = image;
    config.metadata.bazarrImageId = await imageId(image);
  }

  const external = process.env.BAZARR_E2E_URL;
  if (!external) return;
  // A URL that does not parse was already refused by basePath() above,
  // which parses it for its path; one whose path disagrees with the
  // configured base was refused there too, before any login is tried
  // against the wrong page.
  const baseURL = externalBaseURL(external);
  if (credentials() === null) return;

  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      baseURL,
      locale: "en-US",
    });
    await loginIfAsked(await context.newPage());
    // Only the session cookie. The app writes to local storage as soon as it
    // loads (What's New marks the release seen when it opens), and carrying
    // that into every context would hide first-load behaviour from the specs.
    const path = join(runDir, "login.json");
    const { cookies } = await context.storageState();
    await writeFile(path, JSON.stringify({ cookies, origins: [] }));
    process.env.BAZARR_E2E_STORAGE_STATE = path;
  } finally {
    await browser.close();
  }
}
