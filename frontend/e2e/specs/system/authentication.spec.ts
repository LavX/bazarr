/**
 * Form authentication: the session and the login lockout. Pages ask for a
 * login once form authentication is on, a signed-in browser stays signed in
 * across reloads, signing out ends the session, and five wrong passwords
 * lock the account out with the reason, the right password included.
 *
 * Stateful: it turns authentication on with its own throwaway credentials,
 * so it never runs against a real install. The lockout is in memory and dies
 * with the container. Every test of this file runs on the same container,
 * and each one saves the credentials it needs in its own beforeEach.
 */
import { expect, test } from "@e2e/fixtures";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { Page } from "@playwright/test";

const USERNAME = "e2e-admin";
const PASSWORD = "e2e-only-password";

/** The account endpoint, as the app's own client calls it. */
const ACCOUNT = "/api/system/account";

/** The login form's password field. Password inputs carry no textbox role. */
function passwordField(page: Page) {
  return page.getByPlaceholder("Password");
}

test.describe("form authentication", { tag: ["@auth", "@stateful"] }, () => {
  test.beforeEach(async ({ api, bazarr, page }) => {
    await skipWhatsNew(page);
    await completeOnboarding(bazarr);
    // The plaintext password, the way the settings page sends it: the save
    // hashes it itself whenever the value differs from the stored one.
    const saved = await api.post("/api/system/settings", {
      multipart: {
        "settings-auth-type": "form",
        "settings-auth-username": USERNAME,
        "settings-auth-password": PASSWORD,
      },
    });
    expect(saved.ok()).toBe(true);
  });

  test("a session signs in, survives a reload and ends on sign-out", async ({
    page,
  }) => {
    // No session yet: the app asks for the login instead of its pages.
    await page.goto("/");
    const password = passwordField(page);
    await expect(password).toBeVisible();
    await page.getByRole("textbox", { name: "Username" }).fill(USERNAME);
    await password.fill(PASSWORD);
    await page.getByRole("button", { name: "Login" }).click();
    await expect(
      page.getByRole("button", { name: "Jobs Manager" }),
    ).toBeVisible();

    // The signed-in browser stays signed in across a reload.
    await page.reload();
    await expect(
      page.getByRole("button", { name: "Jobs Manager" }),
    ).toBeVisible();
    await expect(password).toHaveCount(0);

    // Sign out through the app's own control. The Controls menu carries a
    // Logout item whenever authentication is form-based, which it is here.
    // The menu sits at the very bottom of the window, where its last item
    // can fall outside the viewport, so it is driven by keyboard: focus,
    // then Enter, the way a keyboard user reaches it.
    await page.getByRole("button", { name: "App controls" }).click();
    const logout = page.getByRole("menuitem", { name: "Logout" });
    await logout.focus();
    await logout.press("Enter");

    // The session is over: the login comes back on its own, and the next
    // page asks for it again.
    await expect(password).toBeVisible();
    await page.goto("/");
    await expect(passwordField(page)).toBeVisible();
  });

  test("five wrong passwords lock the account out, the right one included", async ({
    page,
    api,
  }) => {
    // Five failures from the same client, one per attempt, each refused.
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      const response = await api.post(ACCOUNT, {
        data: { action: "login", username: USERNAME, password: "not-it" },
      });
      expect(response.status()).toBe(403);
    }

    // The lockout is in front of the credential check now, so the right
    // password is refused too, with the reason.
    const locked = await api.post(ACCOUNT, {
      data: { action: "login", username: USERNAME, password: PASSWORD },
    });
    expect(locked.status()).toBe(429);
    expect(await locked.text()).toContain(
      "Too many failed login attempts. Try again in 5 minutes.",
    );

    // The page says the same thing to a user trying to sign in.
    await page.goto("/");
    const password = passwordField(page);
    await expect(password).toBeVisible();
    await page.getByRole("textbox", { name: "Username" }).fill(USERNAME);
    await password.fill(PASSWORD);
    await page.getByRole("button", { name: "Login" }).click();
    await expect(
      page.getByText(/too many failed login attempts/i),
    ).toBeVisible();
  });
});
