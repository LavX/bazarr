/**
 * Call-site tests for the backup restore action's password confirmation.
 *
 * Why: restoring a backup is gated behind the password confirmation on the
 * backend, so the restore action in the table routes the mutation through the
 * gated action: with login on and no live window the prompt appears after the
 * confirm dialog rather than as a refusal toast, and a cancel stages nothing.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import Table from "@/pages/System/Backups/table";
import { customRender, fireEvent, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const BACKUP: System.Backups = {
  id: 1,
  filename: "bazarr_backup_v1_20260101.zip",
  size: "1.2M",
  date: "2026-01-01",
  type: "automatic",
};

function mockRestoreEndpoint() {
  const restores: Record<string, string>[] = [];
  server.use(
    http.get("/api/system/backups", () => HttpResponse.json({ data: [] })),
    http.patch("/api/system/backups", async ({ request }) => {
      const submitted: Record<string, string> = {};
      for (const [key, value] of (await request.formData()).entries()) {
        submitted[key] = String(value);
      }
      restores.push(submitted);
      return HttpResponse.json({
        restart: true,
        message: "Restore staged; Bazarr will restart to apply it",
      });
    }),
  );
  return restores;
}

function mockFormLogin() {
  server.use(
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: { theme: "auto" },
        auth: { type: "form", username: "admin", password: "***" },
      }),
    ),
    http.post(
      "/api/system/account/confirm",
      () => new HttpResponse(null, { status: 204 }),
    ),
  );
}

async function startRestore(user: ReturnType<typeof userEvent.setup>) {
  // The table's restore icon has no measurable box in jsdom, so userEvent
  // refuses the click on it; the event itself is what is under test.
  const restoreIcon = await screen.findByRole("button", { name: "Restore" });
  fireEvent.click(restoreIcon);
  const dialog = await screen.findByRole("dialog", { name: "Restore Backup" });
  await user.click(within(dialog).getByRole("button", { name: "Restore" }));
}

describe("Backups table and the password confirmation", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
  });

  it("prompts for the password after the restore confirm dialog, then restores", async () => {
    const user = userEvent.setup();
    const restores = mockRestoreEndpoint();
    mockFormLogin();

    customRender(<Table backups={[BACKUP]} />);

    await startRestore(user);

    expect(restores).toEqual([]);
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.type(within(prompt).getByLabelText("Password"), "hunter2");
    await user.click(
      within(prompt).getByRole("button", { name: /^confirm$/i }),
    );

    await waitFor(() => {
      expect(restores).toEqual([{ filename: "bazarr_backup_v1_20260101.zip" }]);
    });
  });

  it("restores without a prompt when no login mode is configured", async () => {
    const user = userEvent.setup();
    const restores = mockRestoreEndpoint();

    customRender(<Table backups={[BACKUP]} />);

    await startRestore(user);

    await waitFor(() => {
      expect(restores).toEqual([{ filename: "bazarr_backup_v1_20260101.zip" }]);
    });
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });
});
