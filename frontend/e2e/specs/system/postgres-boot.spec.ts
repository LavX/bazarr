/**
 * The PostgreSQL variant, selected by the environment: with
 * BAZARR_E2E_POSTGRES set, every container the harness starts boots against a
 * throwaway Postgres. This spec proves the whole path: the instance runs on
 * Postgres, a settings write round-trips through the database, and the Status
 * page names the engine it is on. Without the variant the whole block skips
 * before any fixture runs, so an ordinary run stays on SQLite and boots
 * nothing for this file.
 */
/* eslint-disable camelcase -- API bodies keep their transport field names. */
import { expect, test } from "@e2e/fixtures";
import { postgresVariant } from "@e2e/lib/container";
import { completeOnboarding } from "@e2e/lib/shared";
import { skipWhatsNew } from "@e2e/lib/whatsNew";

test.describe("PostgreSQL boot", { tag: ["@postgres", "@stateful"] }, () => {
  // Describe scope, so an ordinary run never boots a container to skip it.
  test.skip(
    () => !postgresVariant(),
    "Set BAZARR_E2E_POSTGRES=1 to run the suite against a throwaway Postgres",
  );

  test.beforeEach(async ({ bazarr, page }) => {
    await skipWhatsNew(page);
    await completeOnboarding(bazarr);
  });

  test("the instance runs on Postgres and writes through it", async ({
    page,
    api,
  }) => {
    const response = await api.get("/api/system/status");
    expect(response.ok()).toBe(true);
    const { data } = (await response.json()) as {
      data: { database_engine: string };
    };
    // The engine publishes its name through capitalize(), which spells it
    // "Postgresql", so the assertion is on the name, not the casing.
    expect(data.database_engine).toMatch(/postgres/i);
    expect(data.database_engine).not.toMatch(/sqlite/i);

    // A database write, not a configuration file: language profiles live in
    // the database, so saving one and reading it back goes through Postgres.
    const saved = await api.post("/api/system/settings", {
      multipart: {
        "languages-profiles": JSON.stringify([
          {
            profileId: 1,
            name: "E2E Postgres Write",
            cutoff: null,
            items: [
              {
                id: 1,
                language: "en",
                audio_exclude: "False",
                hi: "False",
                forced: "False",
              },
            ],
            mustContain: [],
            mustNotContain: [],
            originalFormat: null,
            tag: null,
          },
        ]),
      },
    });
    expect(saved.status()).toBe(204);
    const profiles = await api.get("/api/system/languages/profiles");
    expect(profiles.ok()).toBe(true);
    const list = (await profiles.json()) as { name: string }[];
    expect(Array.isArray(list)).toBe(true);
    expect(list.map((profile) => profile.name)).toContain("E2E Postgres Write");

    // The page names the engine it runs on, from the same answer.
    await page.goto("/system/status");
    await expect(page.getByText("Database Engine")).toBeVisible();
    await expect(page.getByText(data.database_engine).first()).toBeVisible();
  });
});
