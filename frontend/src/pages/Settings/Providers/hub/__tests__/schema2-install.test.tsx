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
import {
  installSummary,
  plainSummary,
} from "@/components/plugin-consent/fixtures";
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
