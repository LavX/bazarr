/* eslint-disable camelcase -- transport names. */
import { describe, expect, it, vi } from "vitest";
import type {
  ProviderHubCatalogEntry,
  ProviderHubInstallation,
} from "@/apis/raw/providerHub";
import { customRender, screen } from "@/tests";
import { CatalogCard } from "./CatalogCard";

const entry = {
  source: "official",
  provider_id: "someprovider",
  name: "Some Provider",
  version: "1.0.0",
  trusted: true,
  manifest: {
    schema_version: 1,
    provider_id: "someprovider",
    name: "Some Provider",
    version: "1.0.0",
  },
} as unknown as ProviderHubCatalogEntry;

const running = {
  provider_id: "someprovider",
  name: "Some Provider",
  active_version: "1.0.0",
  state: "active",
  needs_restart: true,
  trusted: true,
  source_id: "official",
  source_bound: true,
} as unknown as ProviderHubInstallation;

const show = (installed: ProviderHubInstallation) =>
  customRender(
    <CatalogCard
      entry={entry}
      installed={installed}
      onInstall={vi.fn()}
      isInstalling={false}
    />,
  );

describe("the status of a revoked provider on its catalog card", () => {
  it("says revoked, with the reason, instead of Active, while the restart that stops it is still to come", () => {
    show({
      ...running,
      revoked: true,
      revoked_reason: "Version 1.0.0 was revoked. Restart Bazarr+ to stop it.",
    });

    const badge = screen.getByRole("status", { name: /^Revoked \(danger\)/ });
    expect(badge).toHaveAccessibleName(/Restart Bazarr\+ to stop it\./);
    expect(screen.queryByText("Active")).not.toBeInTheDocument();
  });

  it("falls back to the held-back reason when the revoked reason is not set", () => {
    show({ ...running, revoked: true, blocked_reason: "Held back." });
    expect(
      screen.getByRole("status", { name: /^Revoked \(danger\)/ }),
    ).toHaveAccessibleName(/Held back\./);
  });

  it("keeps the usual badge for a provider that is not revoked", () => {
    show(running);
    expect(screen.queryByText("Revoked")).not.toBeInTheDocument();
    expect(screen.getByText("Restart required")).toBeVisible();
  });
});
