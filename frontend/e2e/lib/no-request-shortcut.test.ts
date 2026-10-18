/**
 * The lint rule that closes the request shortcuts and the unguarded
 * context creation a spec could take around the api fixture, checked with
 * ESLint's own RuleTester: the calls it flags, the members it leaves
 * alone, and where the suite's own files may create their contexts.
 */
import { noRequestShortcut } from "@e2e/lint/no-request-shortcut.mts";
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

const ruleTester = new RuleTester({
  languageOptions: { ecmaVersion: "latest", sourceType: "module" },
});

describe("the no-request-shortcut rule", () => {
  it("flags the request shortcuts and the creation of a context outside the fixtures", () => {
    ruleTester.run("no-request-shortcut", noRequestShortcut, {
      valid: [
        { code: "await api.get(appPath('/api/system/settings'))" },
        // The Request object of an answer, not a shortcut that sends.
        { code: "const request = response.request()" },
        // A page of the context the fixtures watch, under any name.
        { code: "const second = await context.newPage()" },
        // The same context through page.context(): a page of it is
        // watched like any other.
        { code: "const second = await page.context().newPage()" },
        // A spec's own client connecting, a database client connecting to
        // a PostgreSQL install, say, is no browser.
        { code: "await client.connect()" },
        // The suite's own fixture and its login, created where they live.
        {
          code: "const api = await playwright.request.newContext()",
          filename: "e2e/fixtures/index.ts",
        },
        {
          code: "const context = await browser.newContext()",
          filename: "e2e/global-setup.ts",
        },
        // The login browser of the global setup, launched and imported
        // where it lives.
        {
          code: "import { chromium } from '@playwright/test'; await chromium.launch()",
          filename: "e2e/global-setup.ts",
        },
      ],
      invalid: [
        {
          code: "await page.request.get('/api/system/settings')",
          errors: [{ messageId: "requestShortcut" }],
        },
        {
          code: "await context.request.post('/settings', { data: form })",
          errors: [{ messageId: "requestShortcut" }],
        },
        // A request context of the spec's own: the base URL, no guard.
        {
          code: "await playwright.request.newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "const own = await browser.newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "await browser.newPage()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The same through the request export of the module: the fixture's
        // playwright.request and it are the one object.
        {
          code: "import { request } from '@playwright/test'; await request.newContext()",
          errors: [
            { messageId: "unguardedImport" },
            { messageId: "unguardedCreation" },
          ],
        },
        // The destructured form: the name is new, the call is the same one.
        {
          code: "const { request: r } = playwright; await r.newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The chained form, however long the chain to the browser.
        {
          code: "await page.context().browser().newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // A browser fixture renamed: the method names it, not the name.
        {
          code: "const own = await myBrowser.newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The computed member: the same method reached through brackets,
        // where the property is a literal, not an identifier.
        {
          code: "const own = await browser['newContext']()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The creation methods destructured out of a browser: the key
        // names the method, whatever the spec binds it to.
        {
          code: "const { newContext } = browser; const own = await newContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The same through a computed key: the literal in the brackets
        // names the method the dot form would, whatever it binds to.
        {
          code: "const { ['newContext']: create } = browser; await create.call(browser)",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The engine members themselves, however the spec reaches them:
        // a browser factory through playwright, under any of its names.
        {
          code: "await playwright.chromium.launch()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "await playwright.browserType.launch()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "await playwright.chromium.connect(wsEndpoint)",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The engines destructured out of playwright, renamed or not:
        // the key is what names the factory.
        {
          code: "const { chromium } = playwright; await chromium.launch()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "const { firefox: f } = playwright; await f.launch()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "const { webkit: w } = playwright; await w.launch()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // A browser of the spec's own, launched or connected by the
        // method names a spec's own client cannot carry.
        {
          code: "await chromium.launchPersistentContext()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        {
          code: "await chromium.connectOverCDP()",
          errors: [{ messageId: "unguardedCreation" }],
        },
        // The engine imports themselves, whichever package they come from:
        // a renamed import cannot be matched by name at the call.
        {
          code: "import { chromium } from '@playwright/test'",
          errors: [{ messageId: "unguardedImport" }],
        },
        {
          code: "import { firefox } from 'playwright'",
          errors: [{ messageId: "unguardedImport" }],
        },
        {
          code: "import { webkit } from 'playwright-core'",
          errors: [{ messageId: "unguardedImport" }],
        },
        // The request shortcuts are flagged everywhere, the suite's own
        // files included.
        {
          code: "await page.request.get('/api/system/settings')",
          filename: "e2e/fixtures/index.ts",
          errors: [{ messageId: "requestShortcut" }],
        },
      ],
    });
  });
});
