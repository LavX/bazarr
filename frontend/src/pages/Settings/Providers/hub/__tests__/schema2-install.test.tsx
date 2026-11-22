/* eslint-disable camelcase -- transport names. */

/**
 * Installing a schema 2 provider from the Subtitle Hub marketplace.
 *
 * Why: the Subtitle Hub's own install route refuses a provider whose closure adds a plugin of another
 * kind, so such a card must open the consent dialog the Plugin Hub uses and send the digest it shows.
 * A provider that needs no consent installs without a dialog, and a schema 1 provider keeps the
 * direct install.
 */

import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import type { PluginConsentSummary } from "@/apis/raw/plugins";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import {
  installSummary,
  plainSummary,
  updateDelta,
} from "@/components/plugin-consent/fixtures";
import { forgetPasswordConfirmation } from "@/modules/passwordConfirm";
import { MarketplacePanel } from "@/pages/Settings/Providers/hub/MarketplacePanel";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

const officialSource = {
  id: "official",
  name: "Official",
  type: "github",
  url: "https://github.com/LavX/bazarr-provider-catalog",
  enabled: true,
  trusted: true,
  official: true,
  catalog_signature: "verified",
  signature_verified: true,
  last_error: null,
};

const entry = (id: string, schema: 1 | 2) => ({
  source: "official",
  source_name: "Official",
  provider_id: id,
  name: `Provider ${id}`,
  version: "1.0.0",
  trusted: true,
  manifest:
    schema === 2
      ? {
          schema_version: 2,
          id,
          kind: "subtitle_provider",
          name: `Provider ${id}`,
          version: "1.0.0",
          permissions: ["network"],
          endpoints: ["https://api.relay.example:443"],
          requires: ["relay_helper.v1"],
        }
      : {
          schema_version: 1,
          provider_id: id,
          name: `Provider ${id}`,
          version: "1.0.0",
        },
});

const catalog = (id: string, schema: 1 | 2) => ({
  sources: [officialSource],
  entries: [entry(id, schema)],
});

/** What installing the provider adds a plugin of another kind to, so the person has to agree. */
const needsConsent: PluginConsentSummary = {
  ...installSummary,
  plugin: {
    id: "relay",
    name: "Provider relay",
    version: "1.0.0",
    kind: "subtitle_provider",
  },
  digest: "sha256:relay-closure",
};

/** A provider whose closure holds only subtitle providers. */
const noConsent: PluginConsentSummary = {
  ...plainSummary,
  plugin: {
    id: "plain",
    name: "Provider plain",
    version: "1.0.0",
    kind: "subtitle_provider",
  },
  members: [
    {
      ...plainSummary.members[0],
      id: "plain",
      name: "Provider plain",
      kind: "subtitle_provider",
    },
  ],
  digest: "sha256:plain-provider",
};

let calls: Array<{ path: string; body: unknown }>;
let summary: PluginConsentSummary;

beforeEach(() => {
  queryClient.clear();
  calls = [];
  summary = needsConsent;
  server.use(
    http.get("/api/system/settings", () => HttpResponse.json({ general: {} })),
    http.get("/api/system/status", () =>
      HttpResponse.json({ data: { sandbox: { workers_sandboxed: false } } }),
    ),
    http.get("/api/plugins/catalog/consent", ({ request }) => {
      calls.push({
        path: "consent",
        body: Object.fromEntries(new URL(request.url).searchParams),
      });
      return HttpResponse.json(summary);
    }),
    http.post("/api/plugins/installations", async ({ request }) => {
      calls.push({ path: "plugin-install", body: await request.json() });
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
    http.post("/api/provider-hub/installations", async ({ request }) => {
      calls.push({ path: "provider-install", body: await request.json() });
      return HttpResponse.json({ job_id: null }, { status: 202 });
    }),
  );
});

const installed = () => calls.filter((call) => call.path.endsWith("-install"));

describe("installing a schema 2 provider from the Subtitle Hub", () => {
  it("opens the consent dialog when its closure adds a plugin, and installs with the digest it showed", async () => {
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel catalog={catalog("relay", 2)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));

    const dialog = await screen.findByRole("dialog");
    // Nothing was sent to install before the person agreed.
    expect(installed()).toEqual([]);
    expect(
      await within(dialog).findByText("Where it comes from"),
    ).toBeVisible();
    const agree = within(dialog).getByRole("button", { name: "Install" });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);

    await waitFor(() => expect(installed()).toHaveLength(1));
    expect(installed()[0]).toEqual({
      path: "plugin-install",
      body: {
        source: "official",
        id: "relay",
        version: "1.0.0",
        consent: "sha256:relay-closure",
      },
    });
    expect(calls.some((call) => call.path === "provider-install")).toBe(false);
  });

  it("shows a refusal in the dialog, in the server's words, and keeps it open", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        calls.push({ path: "plugin-install", body: await request.json() });
        return HttpResponse.json(
          "What this install would add has changed since it was shown to you. Review it again before you agree.",
          { status: 409 },
        );
      }),
    );
    customRender(
      <MarketplacePanel catalog={catalog("relay", 2)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    const dialog = await screen.findByRole("dialog");
    const agree = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);

    expect(
      await within(dialog).findByText(/has changed since it was shown to you/),
    ).toBeVisible();
    expect(screen.getByRole("dialog")).toBe(dialog);
    expect(installed()).toHaveLength(1);
  });

  it("installs at once, with no dialog, when it needs no consent", async () => {
    summary = noConsent;
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel catalog={catalog("plain", 2)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));

    await waitFor(() => expect(installed()).toHaveLength(1));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(installed()[0].body).toMatchObject({
      source: "official",
      id: "plain",
      consent: "sha256:plain-provider",
    });
  });

  it("keeps the direct install for a schema 1 provider and never asks for consent", async () => {
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel catalog={catalog("legacy", 1)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));

    await waitFor(() => expect(installed()).toHaveLength(1));
    expect(installed()[0]).toEqual({
      path: "provider-install",
      body: { source: "official", provider_id: "legacy", version: "1.0.0" },
    });
    expect(calls.some((call) => call.path === "consent")).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

const installedRow = (
  id: string,
  over: Partial<ProviderHubInstallation> = {},
) =>
  ({
    provider_id: id,
    name: `Provider ${id}`,
    active_version: "0.9.0",
    state: "active",
    trusted: true,
    source_id: "official",
    source_bound: true,
    manifest: { schema_version: 1, provider_id: id, version: "0.9.0" },
    ...over,
  }) as unknown as ProviderHubInstallation;

describe("updating an installed provider whose catalog entry is schema 2", () => {
  it("applies an ordinary provider update when the server has no consent step for it", async () => {
    summary = noConsent;
    server.use(
      http.get("/api/plugins/updates/consent", () => {
        calls.push({ path: "update-consent", body: null });
        return HttpResponse.json("No update is available.", { status: 404 });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("plain", 2)}
        providers={[installedRow("plain")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    await waitFor(() => expect(installed()).toHaveLength(1));
    expect(calls.some((call) => call.path === "update-consent")).toBe(true);
    expect(installed()[0].body).toMatchObject({
      source: "official",
      id: "plain",
      version: "1.0.0",
      consent: "sha256:plain-provider",
    });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("opens the update dialog when the server has a delta that needs consent", async () => {
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json({ ...updateDelta, needs_consent: true }),
      ),
      http.get("/api/plugins/installations/relay", () =>
        HttpResponse.json(installedRow("relay")),
      ),
      http.post("/api/plugins/updates/apply", async ({ request }) => {
        calls.push({ path: "apply", body: await request.json() });
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("relay", 2)}
        providers={[installedRow("relay")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    const dialog = await screen.findByRole("dialog");
    expect(calls.some((call) => call.path === "apply")).toBe(false);
    const agree = await within(dialog).findByRole("button", {
      name: "Update",
    });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);
    await waitFor(() =>
      expect(calls.some((call) => call.path === "apply")).toBe(true),
    );
    expect(installed()).toEqual([]);
  });

  it("asks for consent when the specific catalog entry is a replacement that adds access", async () => {
    summary = {
      ...noConsent,
      needs_consent: true,
      destinations: ["https://subtitles.example.org:443"],
      digest: "sha256:provider-replacement",
    };
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json("No update is available", { status: 404 }),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("plain", 2)}
        providers={[installedRow("plain")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));
    const dialog = await screen.findByRole("dialog");
    const destination = await within(dialog).findByText(
      "https://subtitles.example.org:443",
    );
    await waitFor(() => expect(destination).toBeVisible());
    expect(installed()).toEqual([]);
    const agree = within(dialog).getByRole("button", { name: "Update" });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);

    await waitFor(() => expect(installed()).toHaveLength(1));
    expect(installed()[0].body).toMatchObject({
      source: "official",
      id: "plain",
      version: "1.0.0",
      consent: summary.digest,
    });
  });
});

describe("the probe for an update's consent", () => {
  it("applies the update with the digest, and no dialog, when the delta needs no consent", async () => {
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json({ ...updateDelta, needs_consent: false }),
      ),
      http.post("/api/plugins/updates/apply", async ({ request }) => {
        calls.push({ path: "apply", body: await request.json() });
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("relay", 2)}
        providers={[installedRow("relay")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    await waitFor(() =>
      expect(calls.find((call) => call.path === "apply")?.body).toEqual({
        id: "relay",
        consent: updateDelta.digest,
      }),
    );
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(installed()).toEqual([]);
  });

  it("tells the person and sends nothing when the probe fails with anything but a 404", async () => {
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json("The catalog could not be read.", { status: 500 }),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("plain", 2)}
        providers={[installedRow("plain")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    expect(
      await screen.findByText("Provider plain was not updated"),
    ).toBeVisible();
    expect(screen.getByText("The catalog could not be read.")).toBeVisible();
    expect(installed()).toEqual([]);
    expect(calls.some((call) => call.path === "apply")).toBe(false);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    // The card can be tried again.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /^update$/i }),
      ).not.toHaveAttribute("aria-disabled"),
    );
  });
});

describe("an update that goes out as an install of the new release", () => {
  beforeEach(() => {
    summary = noConsent;
    server.use(
      http.get("/api/plugins/updates/consent", () =>
        HttpResponse.json("No update is available.", { status: 404 }),
      ),
    );
  });

  it("says update, not install, when it fails", async () => {
    server.use(
      http.post("/api/plugins/installations", () =>
        HttpResponse.json("The bundle could not be fetched.", { status: 400 }),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("plain", 2)}
        providers={[installedRow("plain")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    expect(
      await screen.findByText("Provider plain was not updated"),
    ).toBeVisible();
    expect(screen.queryByText(/was not installed/)).not.toBeInTheDocument();
  });

  it("asks for the password to update, not to install", async () => {
    forgetPasswordConfirmation();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {},
          auth: { type: "form", username: "admin", password: "***" },
        }),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={catalog("plain", 2)}
        providers={[installedRow("plain")]}
      />,
    );

    await user.click(await screen.findByRole("button", { name: /^update$/i }));

    const prompt = await screen.findByRole("dialog", {
      name: "Confirm your password",
    });
    expect(prompt).toHaveTextContent("to update Provider plain");
    expect(prompt).not.toHaveTextContent(/to install/);
  });

  it("still says install for a first install", async () => {
    forgetPasswordConfirmation();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {},
          auth: { type: "form", username: "admin", password: "***" },
        }),
      ),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel catalog={catalog("plain", 2)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));

    expect(
      await screen.findByRole("dialog", { name: "Confirm your password" }),
    ).toHaveTextContent("to install Provider plain");
  });
});

describe("a second install while one is going ahead", () => {
  it("disables the other Install buttons, with the reason, until it finishes", async () => {
    summary = noConsent;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post("/api/plugins/installations", async ({ request }) => {
        calls.push({ path: "plugin-install", body: await request.json() });
        await gate;
        return HttpResponse.json({ job_id: null }, { status: 202 });
      }),
    );
    const user = userEvent.setup();
    customRender(
      <MarketplacePanel
        catalog={{
          sources: [officialSource],
          entries: [entry("plain", 2), entry("other", 2)],
        }}
        providers={[]}
      />,
    );

    const buttons = await screen.findAllByRole("button", {
      name: /^install$/i,
    });
    await user.click(buttons[0]);

    await waitFor(() => expect(installed()).toHaveLength(1));
    const waiting = screen.getByTitle(
      "Another plugin is being changed. Wait for it to finish.",
    );
    // Still in the tab order, and the reason is read to a screen reader, not only shown on hover.
    expect(waiting).toHaveAttribute("aria-disabled", "true");
    expect(waiting).toHaveAccessibleName(/install/i);
    expect(waiting).toHaveAccessibleDescription(
      "Another plugin is being changed. Wait for it to finish.",
    );
    await user.click(waiting);
    expect(installed()).toHaveLength(1);

    release();
    await waitFor(() =>
      expect(
        screen.queryByTitle(
          "Another plugin is being changed. Wait for it to finish.",
        ),
      ).not.toBeInTheDocument(),
    );
  });
});

describe("focus after the dialog", () => {
  it("goes to the search box when the install replaced the button that opened it", async () => {
    const user = userEvent.setup();
    const { rerender } = customRender(
      <MarketplacePanel catalog={catalog("relay", 2)} providers={[]} />,
    );

    await user.click(await screen.findByRole("button", { name: /^install$/i }));
    const dialog = await screen.findByRole("dialog");
    const agree = await within(dialog).findByRole("button", {
      name: "Install",
    });
    await waitFor(() => expect(agree).toBeEnabled());
    await user.click(agree);
    await waitFor(() => expect(installed()).toHaveLength(1));
    // The server now lists the install, so the card has no Install button any more.
    rerender(
      <MarketplacePanel
        catalog={catalog("relay", 2)}
        providers={[
          installedRow("relay", {
            active_version: undefined,
            staged_version: "1.0.0",
            state: "staged",
            needs_restart: true,
          }),
        ]}
      />,
    );

    await waitFor(() =>
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(
        screen.getByRole("textbox", { name: "Search marketplace" }),
      ).toHaveFocus(),
    );
  });
});
