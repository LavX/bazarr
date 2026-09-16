/**
 * Drives a real Bazarr+ with no Sonarr, Radarr or Sportarr connected.
 *
 * The page it checks is the one an install with nothing connected actually
 * gets: the global catalog opens the page, no panel describes a library that
 * is not there, and the first-run wizard can be walked to its end without
 * connecting anything. Run it through discover_no_instance.py, which boots the
 * backend it drives on a fresh configuration directory.
 *
 * Usage: node discover_no_instance.mjs <base-url> <screenshot-dir>
 */
import { mkdirSync } from "node:fs";
import { createRequire } from "node:module";

// Playwright is not a dependency of this project, so it is resolved from
// wherever it was installed rather than from beside this file.
const modules = process.env.PLAYWRIGHT_NODE_PATH;
const requireFrom = createRequire(
  modules ? `${modules.replace(/\/?$/, "/")}` : import.meta.url,
);
const { chromium } = requireFrom("playwright");

const base = process.argv[2];
const out = process.argv[3];
if (!base || !out) {
  console.error("usage: node discover_no_instance.mjs <base-url> <out-dir>");
  process.exit(2);
}
mkdirSync(out, { recursive: true });

/** Noise this page produces on every install, with or without a library. */
const IGNORED_CONSOLE = [
  // Recharts measures its container before the layout settles.
  /width\(0\) and height\(0\) of chart should be greater than 0/,
  // The startup supervisor stream is only mounted while the backend boots.
  /_supervisor\/events/,
  // Third-party artwork the metadata feeds link to may not resolve offline.
  /Failed to load resource/,
];

/** More than the wizard has steps, so a stuck step ends the walk, not a loop. */
const ONBOARDING_STEP_LIMIT = 20;

const problems = [];
const failures = [];

function check(condition, description) {
  if (condition) {
    console.log(`  ok   ${description}`);
  } else {
    console.log(`  FAIL ${description}`);
    failures.push(description);
  }
}

/** The page must never scroll sideways, at any width it claims to support. */
async function horizontalOverflow(page) {
  return page.evaluate(
    () =>
      document.documentElement.scrollWidth -
      document.documentElement.clientWidth,
  );
}

/**
 * Let the global feeds paint before a screenshot, where the machine running
 * this can reach them. A screenshot is not an assertion, so an install with no
 * metadata reachable simply photographs the loading copy and carries on.
 */
async function settleFeeds(page) {
  await page
    .getByRole("heading", { name: /Trending this week/ })
    .waitFor({ state: "visible", timeout: 10000 })
    .catch(() => undefined);
  await page.waitForTimeout(2500);
}

async function shoot(page, name) {
  await page.screenshot({ path: `${out}/${name}.png`, fullPage: true });
  console.log(`  shot ${out}/${name}.png`);
}

/** Whatever the summary says about itself, read the way the app reads it. */
async function summaryState(page) {
  return page.evaluate(async () => {
    const key = window.Bazarr?.apiKey;
    const response = await fetch("/api/discover/summary", {
      headers: key ? { "X-API-KEY": key } : {},
    });
    if (!response.ok) return { status: response.status };
    const body = await response.json();
    return { status: response.status, state: body.state };
  });
}

/** The library half, named by the headings only it renders. */
const LIBRARY_HEADINGS = [
  "Your library",
  "Needs attention",
  "Still missing",
  "Recently fetched",
];

async function assertGlobalOnly(page, label) {
  await page
    .getByRole("heading", { name: "Beyond your library" })
    .waitFor({ state: "visible", timeout: 30000 });
  check(true, `${label}: the global catalog hero is on the page`);
  for (const heading of LIBRARY_HEADINGS) {
    const count = await page.getByText(heading, { exact: true }).count();
    check(count === 0, `${label}: no "${heading}" section`);
  }
}

const browser = await chromium.launch();
const context = await browser.newContext({
  viewport: { width: 1280, height: 900 },
});
const page = await context.newPage();
page.on("console", (message) => {
  if (message.type() !== "error") return;
  const text = message.text();
  if (IGNORED_CONSOLE.some((pattern) => pattern.test(text))) return;
  problems.push(`console: ${text}`);
});
page.on("pageerror", (error) => problems.push(`pageerror: ${error.message}`));

try {
  console.log("1. the app opens");
  await page.goto(base, { waitUntil: "load" });
  await page.waitForURL(/\/discover$/, { timeout: 30000 });
  check(true, "the app opens on Discover");

  const summary = await summaryState(page);
  check(summary.status === 200, `the summary answers 200 (got ${summary.status})`);
  check(
    summary.state === "new_installation",
    `the summary reports state new_installation (got ${summary.state})`,
  );

  console.log("2. an install with no library opens on the global catalog");
  await assertGlobalOnly(page, "1280px");
  check(
    (await page.getByRole("link", { name: "Connect a library" }).count()) === 1,
    "1280px: one line offers to connect a library",
  );
  await settleFeeds(page);
  await shoot(page, "discover-no-instance-1280");
  check(
    (await horizontalOverflow(page)) === 0,
    "1280px: nothing scrolls sideways",
  );

  await page.setViewportSize({ width: 400, height: 900 });
  await page.waitForTimeout(500);
  await assertGlobalOnly(page, "400px");
  await shoot(page, "discover-no-instance-400");
  check(
    (await horizontalOverflow(page)) === 0,
    "400px: nothing scrolls sideways",
  );
  await page.setViewportSize({ width: 1280, height: 900 });

  console.log("3. the first-run wizard walks to its end without a library");
  await page.goto(`${base}/setup`, { waitUntil: "load" });
  // Each step is taken by whichever of these it offers, preferring the one
  // that connects nothing: this walk is the no-library path through the
  // wizard, not a tour of its forms.
  const ACTIONS = [
    "Get started",
    // The one state the providers step cannot otherwise be answered from.
    "Continue without providers",
    "Skip this step",
    "Skip for now",
    "Skip",
    "Continue without a server",
    "Continue",
    // The last step names where it is sending the reader on this path.
    "Finish and open Discover",
    "Finish",
  ];
  const seen = [];
  for (let taken = 0; taken < ONBOARDING_STEP_LIMIT; taken += 1) {
    const label = (await page.locator("header").first().innerText()).trim();
    seen.push(label.replace(/\s+/g, " "));
    // The wizard asks which path to walk before anything else. This walk is
    // the one an install with no Sonarr, Radarr or Sportarr takes.
    const discoverPath = page.getByRole("radio", {
      name: /find subtitles for anything/i,
    });
    if (await discoverPath.count()) await discoverPath.first().check();
    // The languages step will not advance until something is picked, and a
    // profile is the one thing this walk has to leave behind.
    const languages = page.getByLabel("Languages", { exact: true });
    if (await languages.count()) {
      await languages.first().click();
      const option = page.getByRole("option").first();
      if (await option.count()) await option.click();
      await page.keyboard.press("Escape");
    }
    let clicked = null;
    for (const name of ACTIONS) {
      const button = page.getByRole("button", { name, exact: true });
      if ((await button.count()) && (await button.first().isEnabled())) {
        await button.first().click();
        clicked = name;
        break;
      }
    }
    if (clicked === null) {
      problems.push(
        `the wizard offered no way forward on: ${seen[seen.length - 1]}`,
      );
      break;
    }
    console.log(`  step ${taken + 1}: ${clicked}`);
    await page.waitForTimeout(600);
    if (/\/discover$/.test(new URL(page.url()).pathname)) break;
  }
  check(
    /\/discover$/.test(new URL(page.url()).pathname),
    `the wizard ends on Discover (ended on ${new URL(page.url()).pathname})`,
  );

  console.log("4. the finished install still opens on the global catalog");
  await page.waitForTimeout(1500);
  await assertGlobalOnly(page, "after the wizard");
  await shoot(page, "discover-after-wizard-1280");
  check(
    (await horizontalOverflow(page)) === 0,
    "after the wizard: nothing scrolls sideways",
  );

  check(problems.length === 0, `no console or page errors: ${JSON.stringify(problems)}`);
} finally {
  await browser.close();
}

if (failures.length) {
  console.log(`\nFAILED (${failures.length}):`);
  for (const failure of failures) console.log(`  - ${failure}`);
  process.exit(1);
}
console.log("\nPASSED");
