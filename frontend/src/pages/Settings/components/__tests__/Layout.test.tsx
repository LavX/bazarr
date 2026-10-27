/**
 * Call-site tests for the settings layout's password confirmation.
 *
 * Why: the backend refuses a settings save that changes the post-processing or
 * login keys without a live confirmation, so the layout routes those saves
 * through the gated action. A gated key prompts before the request, an
 * ordinary key saves straight away, and a cancelled prompt during Save and
 * leave keeps the draft and the navigation blocker in place.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import { StrictMode } from "react";
import { createBrowserRouter, Link, RouterProvider } from "react-router";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import Layout from "@/pages/Settings/components/Layout";
import { useFormActions } from "@/pages/Settings/utilities/FormValues";
import { AllProviders } from "@/providers";
import server from "@/tests/mocks/node";

function Stager({ value, label }: { value: LooseObject; label: string }) {
  const { update } = useFormActions();
  return (
    <button type="button" onClick={() => update(value)}>
      {label}
    </button>
  );
}

function renderSettingsPage() {
  const router = createBrowserRouter([
    {
      path: "/",
      element: (
        <Layout name="General">
          <Stager
            value={{ "settings-auth-password": "a-new-password" }}
            label="Stage a gated key"
          />
          <Stager
            value={{ "settings-general-theme": "dark" }}
            label="Stage an ordinary key"
          />
          <Link to="/elsewhere">Leave</Link>
        </Layout>
      ),
    },
    {
      path: "/elsewhere",
      element: <div>The other page</div>,
    },
  ]);
  render(
    <StrictMode>
      <AllProviders>
        <RouterProvider router={router} />
      </AllProviders>
    </StrictMode>,
  );
}

const FORM_AUTH_SETTINGS = http.get("/api/system/settings", () =>
  HttpResponse.json({
    general: { theme: "auto" },
    auth: { type: "form", username: "admin", password: "***" },
  }),
);

// The app shell asks for the system status on mount; it plays no part here.
const MOCK_STATUS = http.get("/api/system/status", () =>
  HttpResponse.json({ mocked: true }),
);

function mockConfirmEndpoint() {
  return http.post(
    "/api/system/account/confirm",
    () => new HttpResponse(null, { status: 204 }),
  );
}

function recordSettingsSaves() {
  const saved: LooseObject[] = [];
  server.use(
    http.post("/api/system/settings", async ({ request }) => {
      const submitted: Record<string, string> = {};
      for (const [key, value] of (await request.formData()).entries()) {
        submitted[key] = String(value);
      }
      saved.push(submitted);
      return new HttpResponse(null, { status: 204 });
    }),
  );
  return saved;
}

async function confirmThePassword(user: ReturnType<typeof userEvent.setup>) {
  const prompt = await screen.findByRole("dialog", {
    name: "Confirm your password",
  });
  await user.type(within(prompt).getByLabelText("Password"), "hunter2");
  await user.click(within(prompt).getByRole("button", { name: /^confirm$/i }));
}

describe("Settings layout and the password confirmation", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
    server.use(FORM_AUTH_SETTINGS, MOCK_STATUS, mockConfirmEndpoint());
  });

  it("prompts before saving a gated key, and saves once confirmed", async () => {
    const user = userEvent.setup();
    const saved = recordSettingsSaves();
    renderSettingsPage();

    await user.click(await screen.findByText("Stage a gated key"));
    await user.click(
      await screen.findByRole("button", { name: /save 1 pending change/i }),
    );

    expect(saved).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(saved).toHaveLength(1);
    });
    expect(saved[0]["settings-auth-password"]).toBe("a-new-password");
  });

  it("saves an ordinary key without a prompt", async () => {
    const user = userEvent.setup();
    const saved = recordSettingsSaves();
    renderSettingsPage();

    await user.click(await screen.findByText("Stage an ordinary key"));
    await user.click(
      await screen.findByRole("button", { name: /save 1 pending change/i }),
    );

    await waitFor(() => {
      expect(saved).toHaveLength(1);
    });
    expect(saved[0]["settings-general-theme"]).toBe("dark");
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });

  it("keeps the draft and the blocker when Save and leave is cancelled at the prompt", async () => {
    const user = userEvent.setup();
    const saved = recordSettingsSaves();
    renderSettingsPage();

    await user.click(await screen.findByText("Stage a gated key"));
    await user.click(screen.getByRole("link", { name: "Leave" }));

    // The navigation blocker asks what to do with the unsaved gated change.
    expect(await screen.findByText("Unsaved Changes")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /save and leave/i }));

    // The gated save opens the password prompt; cancelling it keeps the draft.
    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    await user.click(within(prompt).getByRole("button", { name: /cancel/i }));

    await waitFor(() => {
      expect(
        screen.queryByText("Confirm your password"),
      ).not.toBeInTheDocument();
    });
    expect(saved).toEqual([]);
    // Still on the settings page, with the draft and the blocker in place.
    expect(screen.queryByText("The other page")).not.toBeInTheDocument();
    expect(screen.getByText("Unsaved Changes")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /save 1 pending change/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /save and leave/i }),
    ).toBeInTheDocument();
  });
});
