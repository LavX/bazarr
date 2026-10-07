/**
 * The lint rule of the sub-path lane, checked with ESLint's own RuleTester:
 * one test per flagged form, and the legitimate strings it must leave alone.
 */
import { noRootRelativeUrl } from "@e2e/lint/no-root-relative-url.mts";
import { RuleTester } from "eslint";
import { describe, it } from "vitest";

const ruleTester = new RuleTester({
  languageOptions: { ecmaVersion: "latest", sourceType: "module" },
});

describe("the no-root-relative-url rule", () => {
  it("flags a leading-slash literal in page.goto", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [{ code: 'page.goto(appPath("/settings"));' }],
      invalid: [
        {
          code: 'page.goto("/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
      ],
    });
  });

  it("flags a leading-slash literal in the request helpers", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [{ code: "api.get(path);" }],
      invalid: [
        {
          code: 'api.get("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.post("/api/system/settings", { multipart: form });',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.put("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.patch("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.delete("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.head("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: 'api.fetch("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          code: "api.get(`/api/system/jobs?id=${id}`);",
          errors: [{ messageId: "rootRelative" }],
        },
      ],
    });
  });

  it("flags a leading-slash literal in expect(...).toHaveURL", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [{ code: "expect(page).toHaveURL(/\\/discover$/);" }],
      invalid: [
        {
          code: 'expect(page).toHaveURL("/settings/general");',
          errors: [{ messageId: "rootRelative" }],
        },
      ],
    });
  });

  it("flags a leading-slash literal in page.waitForURL", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [{ code: "page.waitForURL(/\\/setup\\/welcome$/);" }],
      invalid: [
        {
          code: 'page.waitForURL("/discover");',
          errors: [{ messageId: "rootRelative" }],
        },
      ],
    });
  });

  it("flags a leading-slash first argument of new URL, with or without a base", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [
        { code: 'new URL(appPath("/api/system/settings"), baseURL);' },
        { code: "new URL(instance.baseURL);" },
      ],
      invalid: [
        {
          code: 'new URL("/api/system/settings");',
          errors: [{ messageId: "rootRelative" }],
        },
        {
          // A leading slash replaces the base's whole path, so this drops it.
          code: 'new URL("/api/system/settings", baseURL);',
          errors: [{ messageId: "rootRelative" }],
        },
      ],
    });
  });

  it("leaves alone the legitimate strings of a spec", () => {
    ruleTester.run("no-root-relative-url", noRootRelativeUrl, {
      valid: [
        // A check on a response's URL, not a navigation to it.
        { code: 'expect(url.includes("/api/socket.io/")).toBe(true);' },
        // A media server's stored path mapping, not a URL of the app.
        { code: 'const mapping = { local_path: "/movies" };' },
        // A path built by the suite's own helper.
        { code: 'page.goto(appPath("/system/status"));' },
      ],
      invalid: [],
    });
  });
});
