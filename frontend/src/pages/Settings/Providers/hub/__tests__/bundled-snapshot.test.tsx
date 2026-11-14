/* eslint-disable camelcase */

/**
 * The catalog snapshot shipped in the release image.
 *
 * Why: an entry read from the snapshot is never an update (it is not newer
 * than anything fetched, and the backend has no manifest to apply), and the
 * Sources drawer says plainly when the catalog on screen is the snapshot.
 * The air-gapped case: a provider installed from snapshot N, then an image
 * with snapshot N+1, and the catalog never fetched.
 *
 * Test: Run with `cd frontend && pnpm exec vitest run --reporter=verbose`.
 */

import { describe, expect, it } from "vitest";
import type {
  ProviderHubCatalog,
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { CatalogCard } from "@/pages/Settings/Providers/hub/components/CatalogCard";
import { SourcesDrawer } from "@/pages/Settings/Providers/hub/SourcesDrawer";
import {
  getLatestCatalogEntry,
  isUpdateAvailable,
  summarizeUpdates,
} from "@/pages/Settings/Providers/hub/utils";
import { customRender, screen } from "@/tests";

const manifest = {
  schema_version: 1,
  provider_id: "someprovider",
  name: "Some Provider",
  version: "1.1.0",
};

const installed = {
  provider_id: "someprovider",
  name: "Some Provider",
  active_version: "1.0.0",
  state: "active",
  pending_restart: false,
  trusted: true,
  source_id: "official",
  source_bound: true,
  origin: "catalog",
  manifest,
} as unknown as ProviderHubInstallation;

function entry(extra: Partial<ProviderHubCatalogEntry>) {
  return {
    source: "official",
    provider_id: "someprovider",
    name: "Some Provider",
    version: "1.1.0",
    trusted: true,
    manifest,
    ...extra,
  } as ProviderHubCatalogEntry;
}

function catalogOf(...entries: ProviderHubCatalogEntry[]): ProviderHubCatalog {
  return { sources: [], entries };
}

describe("a bundled entry is never an update", () => {
  const bundled = catalogOf(entry({ origin: "bundled" }));

  it("is not the latest entry of an install's source", () => {
    expect(getLatestCatalogEntry(bundled, "someprovider", "official")).toBe(
      null,
    );
  });

  it("offers no update for an install from an older snapshot", () => {
    expect(isUpdateAvailable(installed, bundled)).toBe(false);
    expect(summarizeUpdates([installed], bundled).available).toEqual([]);
  });

  it("still offers a fetched entry of the same version", () => {
    const fetched = catalogOf(entry({}));
    expect(isUpdateAvailable(installed, fetched)).toBe(true);
    expect(summarizeUpdates([installed], fetched).available).toHaveLength(1);
  });

  it("does not hide the fetched entry behind a bundled one", () => {
    const both = catalogOf(
      entry({ origin: "bundled", version: "1.3.0" }),
      entry({ version: "1.2.0" }),
    );
    expect(
      getLatestCatalogEntry(both, "someprovider", "official")?.version,
    ).toBe("1.2.0");
  });
});

describe("CatalogCard and a bundled entry", () => {
  const noop = () => undefined;

  it("shows Installed, not Update, for a bundled entry of another version", () => {
    customRender(
      <CatalogCard
        entry={entry({ origin: "bundled" })}
        installed={installed}
        onInstall={noop}
        isInstalling={false}
      />,
    );
    expect(screen.queryByRole("button", { name: /update/i })).toBeNull();
    expect(screen.getByRole("button", { name: /installed/i })).toBeDisabled();
  });

  it("shows Update for a fetched entry of another version", () => {
    customRender(
      <CatalogCard
        entry={entry({})}
        installed={installed}
        onInstall={noop}
        isInstalling={false}
      />,
    );
    expect(screen.getByRole("button", { name: /update/i })).toBeEnabled();
  });
});

describe("SourcesDrawer and a bundled catalog", () => {
  const source = {
    id: "official",
    name: "Official",
    type: "github",
    url: "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
    enabled: true,
    official: true,
    trusted: true,
    trust_reason: "release floor",
    last_error: "GitHub could not be reached.",
  };

  it("says the catalog is the bundled one, next to the failed refresh", async () => {
    customRender(
      <SourcesDrawer
        opened
        onClose={() => undefined}
        sources={[{ ...source, catalog_status: "bundled" }]}
        installedSourceUsage={{}}
      />,
    );
    expect(
      await screen.findByText(/catalog bundled with this release/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/GitHub could not be reached/)).toBeInTheDocument();
  });

  it("says nothing about a bundled catalog for a fetched one", async () => {
    customRender(
      <SourcesDrawer
        opened
        onClose={() => undefined}
        sources={[source]}
        installedSourceUsage={{}}
      />,
    );
    await screen.findByText(/GitHub could not be reached/);
    expect(screen.queryByText(/catalog bundled with this release/i)).toBeNull();
  });
});
