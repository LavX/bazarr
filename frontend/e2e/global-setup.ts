import { chromium } from "@playwright/test";
import { mkdtemp, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { credentials, loginIfAsked } from "./lib/auth";

/**
 * Gives the run a private scratch directory the workers can coordinate in.
 *
 * Against an existing instance with a login, it also signs in once for the
 * whole run and leaves the session where the fixtures pick it up, so every
 * browser context starts logged in and the form is only ever filled once.
 */
export default async function globalSetup() {
  const runDir = await mkdtemp(join(tmpdir(), "bazarr-e2e-"));
  process.env.BAZARR_E2E_RUN_DIR = runDir;

  const external = process.env.BAZARR_E2E_URL;
  if (!external || credentials() === null) return;

  const browser = await chromium.launch();
  try {
    const context = await browser.newContext({
      baseURL: external.replace(/\/+$/, ""),
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
