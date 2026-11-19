import { describe, expect, it } from "vitest";
import { createAppTheme } from "@/App/ThemeProvider";

describe("app theme popover default", () => {
  it("keeps the detached-trigger check on outside the test environment", () => {
    const theme = createAppTheme(false);

    expect(theme.components?.Popover?.defaultProps?.hideDetached).toBe(true);
  });

  it("turns the check off in the test environment", () => {
    const theme = createAppTheme(true);

    expect(theme.components?.Popover?.defaultProps?.hideDetached).toBe(false);
  });

  it("leaves the other component defaults alone in both modes", () => {
    for (const testEnv of [false, true]) {
      const { components } = createAppTheme(testEnv);

      expect(components?.Button).toBeDefined();
      expect(components?.Input).toBeDefined();
    }
  });
});
