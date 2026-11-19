import { describe, expect, it } from "vitest";
import { createAppTheme } from "@/App/ThemeProvider";
import actionIconClasses from "@/assets/action_icon.module.scss";
import badgeClasses from "@/assets/badge.module.scss";
import buttonClasses from "@/assets/button.module.scss";
import inputClasses from "@/assets/input.module.scss";
import paginationClasses from "@/assets/pagination.module.scss";
import progressClasses from "@/assets/progress.module.scss";

describe("app theme popover default", () => {
  it("keeps the detached-trigger check on outside the test environment", () => {
    const theme = createAppTheme(false);

    expect(theme.components?.Popover?.defaultProps?.hideDetached).toBe(true);
  });

  it("turns the check off in the test environment", () => {
    const theme = createAppTheme(true);

    expect(theme.components?.Popover?.defaultProps?.hideDetached).toBe(false);
  });

  it("leaves the other component entries as the base theme defines them", () => {
    const expected = {
      ActionIcon: actionIconClasses,
      Badge: badgeClasses,
      Button: buttonClasses,
      Input: inputClasses,
      Pagination: paginationClasses,
      Progress: progressClasses,
    };

    for (const testEnv of [false, true]) {
      const { components } = createAppTheme(testEnv);

      for (const [name, classNames] of Object.entries(expected)) {
        expect(components?.[name]?.classNames).toBe(classNames);
      }
    }
  });

  it("differs between the two modes only in the Popover entry", () => {
    const production = createAppTheme(false).components ?? {};
    const test = createAppTheme(true).components ?? {};

    expect(Object.keys(test).sort()).toEqual(Object.keys(production).sort());
    for (const name of Object.keys(production)) {
      if (name === "Popover") continue;
      expect(test[name]).toBe(production[name]);
    }
  });
});
