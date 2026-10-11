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
import { appPath } from "@e2e/lib/base";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";
import type { Page, Request } from "@playwright/test";

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
    const saved = await api.post(appPath("/api/system/settings"), {
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
    // Every request the page sends from here on sits in a set until it
    // finishes or fails, and the end of this test waits for that set to
    // drain: a read still on its way when the logout fires is the one
    // that can answer after it and re-issue a fresh signed session, and
    // the session only exists after the sign-in below, so every read
    // that can carry it is counted from its own start. The listeners
    // only watch; they send nothing.
    const outstanding = new Set<Request>();
    page.on("request", (request) => outstanding.add(request));
    page.on("requestfinished", (request) => outstanding.delete(request));
    page.on("requestfailed", (request) => outstanding.delete(request));

    // No session yet: the app asks for the login instead of its pages.
    await page.goto(appPath("/"));
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
    // The reads still on their way are waited out first: the one that
    // answers last can re-issue a fresh signed session into the jar after
    // the cookie was already seen gone, and the set only drains, so the
    // wait itself cannot bring the session back.
    await expect.poll(() => outstanding.size).toBe(0);
    // The jar is watched until the session cookie is really gone, and a
    // goto that races a resurrected cookie renders the signed-in app.
    // Waiting first keeps the last assertion about the signed-out state
    // rather than about the race resolving favorably on its own.
    await expect
      .poll(async () =>
        (await page.context().cookies()).filter(
          (cookie) => cookie.name === "session",
        ),
      )
      .toEqual([]);
    await page.goto(appPath("/"));
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
    await page.goto(appPath("/"));
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
