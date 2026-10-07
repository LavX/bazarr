/**
 * The base-path helpers of the sub-path lane, with the run's two sources of
 * the base stood in for by the environment. Nothing here starts a container.
 */
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { appPath, basePath } from "./base";

// The run's own sources are the environment, so each test starts from the
// shell's, not from whatever the last test or the shell happened to leave.
beforeEach(() => {
  delete process.env.BAZARR_E2E_BASE_PATH;
  delete process.env.BAZARR_E2E_URL;
});

afterEach(() => {
  delete process.env.BAZARR_E2E_BASE_PATH;
  delete process.env.BAZARR_E2E_URL;
});

describe("the configured base path", () => {
  it("is empty when nothing configures one, so every path keeps its shape", () => {
    expect(basePath()).toBe("");
    expect(appPath("/api/system/settings")).toBe("/api/system/settings");
    expect(appPath("/")).toBe("/");
  });

  it("is the path the containers are told to serve under", () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    expect(basePath()).toBe("/bazarr");
    expect(appPath("/system/status")).toBe("/bazarr/system/status");
    expect(appPath("/")).toBe("/bazarr/");
  });

  it("loses a trailing slash and refuses a value that is not a path", () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr/";
    expect(basePath()).toBe("/bazarr");

    process.env.BAZARR_E2E_BASE_PATH = "bazarr";
    expect(() => basePath()).toThrow(/BAZARR_E2E_BASE_PATH/);

    process.env.BAZARR_E2E_BASE_PATH = "//bazarr";
    expect(() => basePath()).toThrow(/BAZARR_E2E_BASE_PATH/);
  });

  it("is the external URL's own path, and refuses a base that disagrees", () => {
    process.env.BAZARR_E2E_URL = "https://bazarr.example.lan/bazarr";
    expect(basePath()).toBe("/bazarr");
    expect(appPath("/discover")).toBe("/bazarr/discover");

    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    expect(basePath()).toBe("/bazarr");

    process.env.BAZARR_E2E_BASE_PATH = "";
    expect(() => basePath()).toThrow(/disagree/);
  });

  it("refuses an appPath that is not a path from the app's root", () => {
    process.env.BAZARR_E2E_BASE_PATH = "/bazarr";
    expect(() => appPath("settings/general")).toThrow(/app's root/);
  });
});
