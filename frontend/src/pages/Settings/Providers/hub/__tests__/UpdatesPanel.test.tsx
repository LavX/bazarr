/* eslint-disable camelcase */

/**
 * Call-site tests for the update actions in the Provider Hub updates panel.
 *
 * Why: applying a plugin update is gated behind the password confirmation on
 * the backend, so the panel routes both the single update and the whole-batch
 * "Apply all" through the gated action: with login on and no live window the
 * prompt appears before the request rather than as a refusal toast after it,
 * and one confirmation covers the batch rather than prompting per provider.
 *
 * Test: Run with `cd frontend && pnpm exec vitest run --reporter=verbose`.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import type {
  ProviderHubCatalog,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import { UpdatesPanel } from "@/pages/Settings/Providers/hub/UpdatesPanel";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const manifest = {
  schema_version: 1,
  provider_id: "someprovider",
  name: "Some Provider",
  version: "1.0.0",
  source: {
    type: "github",
    repo: "LavX/bazarr-provider-catalog",
    ref: "main",
    commit: "a".repeat(40),
    trusted: true,
  },
};

function installed(providerId: string): ProviderHubInstallation {
  return {
    provider_id: providerId,
    name: "Some Provider",
    active_version: "0.9.0",
    staged_version: null,
    state: "active",
    pending_restart: false,
    trusted: true,
    source_id: "official",
    source_bound: true,
    origin: "catalog",
    manifest,
  } as unknown as ProviderHubInstallation;
}

function catalogFor(...providerIds: string[]): ProviderHubCatalog {
  return {
    sources: [
      {
        id: "official",
        name: "Official",
        type: "github",
        url: "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
        enabled: true,
        trusted: true,
        last_error: null,
      },
    ],
    entries: providerIds.map((providerId) => ({
      source: "official",
      provider_id: providerId,
      name: "Some Provider",
      version: "1.0.0",
      trusted: true,
      manifest,
    })),
  } as unknown as ProviderHubCatalog;
}

function mockApplyUpdate() {
  const applied: string[] = [];
  server.use(
    http.post("/api/provider-hub/updates/apply", ({ request }) => {
      const url = new URL(request.url);
      applied.push(url.searchParams.get("provider_id") ?? "");
      // No job to wait on, so the apply settles at once in the test.
      return HttpResponse.json({ job_id: null });
    }),
  );
  return applied;
}

function mockFormLogin() {
  return [
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
  ];
}

async function confirmThePassword(user: ReturnType<typeof userEvent.setup>) {
  const prompt = await screen.findByRole("dialog", {
    name: "Confirm your password",
  });
  await user.type(within(prompt).getByLabelText("Password"), "hunter2");
  await user.click(within(prompt).getByRole("button", { name: /^confirm$/i }));
}

describe("UpdatesPanel and the password confirmation", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
  });

  it("asks for the password before applying a single update", async () => {
    const user = userEvent.setup();
    const applied = mockApplyUpdate();
    server.use(...mockFormLogin());

    customRender(
      <UpdatesPanel
        providers={[installed("someprovider")]}
        catalog={catalogFor("someprovider")}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: /apply update/i }),
    );
    expect(applied).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(applied).toEqual(["someprovider"]);
    });
  });

  it("asks once for the whole batch, not once per provider", async () => {
    const user = userEvent.setup();
    const applied = mockApplyUpdate();
    server.use(...mockFormLogin());

    customRender(
      <UpdatesPanel
        providers={[installed("providerone"), installed("providertwo")]}
        catalog={catalogFor("providerone", "providertwo")}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /apply all/i }));

    // The whole batch waits on the same prompt; neither apply went out yet.
    expect(applied).toEqual([]);
    expect(
      await screen.findByText("Confirm your password"),
    ).toBeInTheDocument();

    await confirmThePassword(user);

    await waitFor(() => {
      expect(applied.sort()).toEqual(["providerone", "providertwo"]);
    });
  });

  it("applies without a prompt when no login mode is configured", async () => {
    const user = userEvent.setup();
    const applied = mockApplyUpdate();

    customRender(
      <UpdatesPanel
        providers={[installed("someprovider")]}
        catalog={catalogFor("someprovider")}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: /apply update/i }),
    );

    await waitFor(() => {
      expect(applied).toEqual(["someprovider"]);
    });
    expect(screen.queryByText("Confirm your password")).not.toBeInTheDocument();
  });
});
