/**
 * Signs in to an instance that has form authentication turned on.
 *
 * The credentials come from BAZARR_E2E_USER and BAZARR_E2E_PASSWORD and are
 * typed into the login form; they are never logged or put in a title.
 */
import type { Page } from "@playwright/test";
import { expect } from "@playwright/test";

export function credentials(): { username: string; password: string } | null {
  const username = process.env.BAZARR_E2E_USER;
  const password = process.env.BAZARR_E2E_PASSWORD;
  if (!username || !password) return null;
  return { username, password };
}

/**
 * Logs in through the form when the app asks for it, and does nothing
 * otherwise. It tries once: a wrong password fails here instead of being
 * retried, because the instance locks the account after five failures.
 */
export async function loginIfAsked(page: Page): Promise<void> {
  // By role: the label matches the "Toggle password visibility" button too,
  // and its required asterisk rules out an exact label match.
  const password = page.getByRole("textbox", { name: "Password" });
  await page.goto("/");
  // The login form, or any screen the app opens on once no login is needed.
  await expect(password.or(page.getByRole("heading").first())).toBeVisible();
  if (!(await password.isVisible())) return;
  const login = credentials();
  if (login === null) {
    throw new Error(
      "this instance asks for a login: set BAZARR_E2E_USER and BAZARR_E2E_PASSWORD",
    );
  }
  await page.getByRole("textbox", { name: "Username" }).fill(login.username);
  await password.fill(login.password);
  await page.getByRole("button", { name: "Login" }).click();
  await expect(password).toBeHidden();
}
