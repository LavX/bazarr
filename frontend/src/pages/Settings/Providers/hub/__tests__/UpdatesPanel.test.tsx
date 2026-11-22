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
import { quietDelta, updateDelta } from "@/components/plugin-consent/fixtures";
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
    needs_restart: false,
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

function schemaTwoCatalog(legacy: string[], schemaTwo: string[]) {
  const catalog = catalogFor(...legacy, ...schemaTwo);
  return {
    ...catalog,
    entries: catalog.entries.map((entry) =>
      schemaTwo.includes(entry.provider_id)
        ? {
            ...entry,
            manifest: {
              schema_version: 2,
              id: entry.provider_id,
              kind: "subtitle_provider",
              version: entry.version,
            },
          }
        : entry,
    ),
  };
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

describe("UpdatesPanel and provider update consent", () => {
  beforeEach(() => {
    forgetPasswordConfirmation();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: {}, auth: { type: null } }),
      ),
    );
  });

  it("shows the added access and applies only after consent with the shown digest", async () => {
    const user = userEvent.setup();
    const legacyApplied = mockApplyUpdate();
    const applied: unknown[] = [];
    const provider = installed("offersub");
    const delta = {
      ...updateDelta,
      plugin: {
        id: "offersub",
        name: "Some Provider",
        from_version: "0.9.0",
        to_version: "1.0.0",
        kind: "subtitle_provider",
      },
      added: {
        ...updateDelta.added,
        destinations: ["https://subtitles.example.org:443"],
        permissions: [{ name: "network", mark: "declared" }],
      },
    };
    server.use(
      http.get("/api/plugins/updates/consent", ({ request }) =>
        new URL(request.url).searchParams.get("id") === "offersub"
          ? HttpResponse.json(delta)
          : HttpResponse.json("No update is available", { status: 404 }),
      ),
      http.get("/api/plugins/installations/offersub", () =>
        HttpResponse.json(provider),
      ),
      http.post("/api/plugins/updates/apply", async ({ request }) => {
        applied.push(await request.json());
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    customRender(
      <UpdatesPanel
        providers={[provider]}
        catalog={schemaTwoCatalog([], ["offersub"])}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: /apply update/i }),
    );
    const dialog = await screen.findByRole("dialog");

    const destination = await within(dialog).findByText(
      "https://subtitles.example.org:443",
    );
    await waitFor(() => expect(destination).toBeVisible());
    expect(applied).toEqual([]);
    expect(legacyApplied).toEqual([]);
    const consent = within(dialog).getByRole("button", { name: "Update" });
    await waitFor(() => expect(consent).toBeEnabled());
    await user.click(consent);

    await waitFor(() =>
      expect(applied).toEqual([{ id: "offersub", consent: delta.digest }]),
    );
    expect(legacyApplied).toEqual([]);
  });

  it("applies the bulk updates that need no consent and leaves the others for individual review", async () => {
    const user = userEvent.setup();
    const applied = mockApplyUpdate();
    server.use(
      http.get("/api/plugins/updates/consent", ({ request }) => {
        const id = new URL(request.url).searchParams.get("id");
        if (id === "changes") return HttpResponse.json(updateDelta);
        if (id === "same") return HttpResponse.json(quietDelta);
        if (id === "no-delta")
          return HttpResponse.json("No update is available", { status: 404 });
        return HttpResponse.json("Could not read the update", { status: 500 });
      }),
    );
    const schemaTwo = ["changes", "same", "no-delta", "failed-preview"];
    customRender(
      <UpdatesPanel
        providers={["legacy", ...schemaTwo].map(installed)}
        catalog={schemaTwoCatalog(["legacy"], schemaTwo)}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /apply all/i }));

    await waitFor(() =>
      expect(applied.sort()).toEqual(["legacy", "no-delta", "same"]),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("UpdatesPanel and a revoked running provider", () => {
  it("offers the update once and does not show the provider as staged", async () => {
    const revoked = {
      ...installed("someprovider"),
      needs_restart: true,
      revoked: true,
    } as ProviderHubInstallation;

    customRender(
      <UpdatesPanel
        providers={[revoked]}
        catalog={catalogFor("someprovider")}
      />,
    );

    expect(
      await screen.findByRole("button", { name: /apply update/i }),
    ).toBeEnabled();
    expect(screen.queryByText("Pending restart")).not.toBeInTheDocument();
    expect(screen.getAllByText("Some Provider")).toHaveLength(1);
  });

  it("keeps a revoked provider with an update already staged as waiting for the restart", async () => {
    const staged = {
      ...installed("someprovider"),
      state: "staged",
      staged_version: "1.0.0",
      needs_restart: true,
      revoked: true,
    } as ProviderHubInstallation;

    customRender(
      <UpdatesPanel
        providers={[staged]}
        catalog={catalogFor("someprovider")}
      />,
    );

    expect(await screen.findByText("Pending restart")).toBeVisible();
    expect(
      screen.queryByRole("button", { name: /apply update/i }),
    ).not.toBeInTheDocument();
  });
});
