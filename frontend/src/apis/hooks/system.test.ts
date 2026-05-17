import { describe, expect, it } from "vitest";
import { getPostLoginRedirectTarget } from "./system";

describe("getPostLoginRedirectTarget", () => {
  it("redirects to root when base URL is empty", () => {
    expect(getPostLoginRedirectTarget("")).toBe("/");
  });

  it("keeps configured base URL", () => {
    expect(getPostLoginRedirectTarget("/bazarr")).toBe("/bazarr");
  });
});
