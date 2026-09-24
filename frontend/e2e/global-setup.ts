import { chromium } from "@playwright/test";
import { mkdtemp } from "node:fs/promises";
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
    const path = join(runDir, "login.json");
    await context.storageState({ path });
    process.env.BAZARR_E2E_STORAGE_STATE = path;
  } finally {
    await browser.close();
  }
}
