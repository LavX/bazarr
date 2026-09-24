/**
 * End-to-end tests against a real Bazarr+ container. See docs/agents/e2e.md.
 *
 * Two projects. `stateful` specs change the instance, so each spec file gets a
 * container of its own; they run one at a time for now, and more workers can
 * be given to them later without changing a spec. `stateless` specs only read,
 * so they run fully parallel against one shared, already onboarded container.
 * A spec is stateful when its describe block carries the @stateful tag.
 */
import { defineConfig, devices } from "@playwright/test";
import { SUITE_VIEWPORT } from "./e2e/lib/fit";

const CI = !!process.env.CI;

export default defineConfig({
  testDir: "./e2e/specs",
  tsconfig: "./e2e/tsconfig.json",
  outputDir: "./e2e-results",
  globalSetup: "./e2e/global-setup.ts",
  globalTeardown: "./e2e/global-teardown.ts",
  // Tight on purpose: a step that needs longer should wait on a condition
  // with its own cap, not stretch every test. Container startup is added on
  // top by the fixture only for the test that pays for it.
  timeout: 60_000,
  expect: { timeout: 5_000 },
  forbidOnly: CI,
  retries: CI ? 1 : 0,
  reporter: [
    ["list"],
    ["html", { outputFolder: "./e2e-report", open: "never" }],
  ],
  use: {
    ...devices["Desktop Chrome"],
    // The full HD window the app is designed for, minus browser chrome. A
    // spec that needs another size sets its own with test.use.
    viewport: SUITE_VIEWPORT,
    // Steps that guess from the browser, like the wizard's languages, guess
    // the same thing on every machine.
    locale: "en-US",
    actionTimeout: 10_000,
    navigationTimeout: 20_000,
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "stateful",
      grep: /@stateful/,
      fullyParallel: false,
      workers: 1,
    },
    {
      name: "stateless",
      grepInvert: /@stateful/,
      fullyParallel: true,
    },
  ],
});
