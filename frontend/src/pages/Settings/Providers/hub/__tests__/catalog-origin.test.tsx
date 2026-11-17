/* eslint-disable camelcase */

/**
 * Which origin a source's catalog came from, as the Sources drawer shows it.
 *
 * Why: when GitHub refuses, the official catalog is read from a mirror. The
 * user should see that plainly, with the time GitHub allows requests again
 * when it said one, and a source read from GitHub should say nothing.
 *
 * Test: Run with `cd frontend && pnpm exec vitest run --reporter=verbose`.
 */

import { describe, expect, it } from "vitest";
import type { ProviderHubCatalogSource } from "@/apis/raw/providerHub";
import { SourcesDrawer } from "@/pages/Settings/Providers/hub/SourcesDrawer";
import { describeCatalogOrigin } from "@/pages/Settings/Providers/hub/utils";
import { customRender, screen } from "@/tests";

const NOW = new Date("2026-11-17T10:00:00Z");

const failure = {
  surface: "pointer",
  origin: "raw",
  kind: "rate_limited",
  status: 429,
  at: "2026-11-17T09:59:00Z",
};

describe("describeCatalogOrigin", () => {
  it("says nothing for a catalog read from GitHub", () => {
    expect(describeCatalogOrigin({ last_origin: "raw" }, NOW)).toBeNull();
    expect(describeCatalogOrigin({}, NOW)).toBeNull();
    expect(
      describeCatalogOrigin({ last_origin: null, last_failure: failure }, NOW),
    ).toBeNull();
  });

  it("names the mirror and the time GitHub allows requests again", () => {
    const text = describeCatalogOrigin(
      {
        last_origin: "pages",
        last_failure: { ...failure, retry_at: "2026-11-17T10:30:00Z" },
      },
      NOW,
    );
    expect(text).toContain("GitHub Pages mirror");
    expect(text).toContain("allows requests again at 2026-11-17 10:30:00 UTC");
  });

  it("names the mirror alone when GitHub gave no reset time", () => {
    const text = describeCatalogOrigin(
      { last_origin: "pages", last_failure: failure },
      NOW,
    );
    expect(text).toContain("GitHub Pages mirror");
    expect(text).not.toContain("allows requests again");
  });

  it("says the limit has passed once the reset time is behind us", () => {
    const text = describeCatalogOrigin(
      {
        last_origin: "pages",
        last_failure: { ...failure, retry_at: "2026-11-17T09:30:00Z" },
      },
      NOW,
    );
    expect(text).toContain("limit has passed");
    expect(text).not.toContain("allows requests again");
  });

  it("ignores a reset time it cannot read", () => {
    const text = describeCatalogOrigin(
      {
        last_origin: "pages",
        last_failure: { ...failure, retry_at: "soon" },
      },
      NOW,
    );
    expect(text).toContain("GitHub Pages mirror");
    expect(text).not.toContain("allows requests again");
  });
});

describe("SourcesDrawer and the mirror", () => {
  const source: ProviderHubCatalogSource = {
    id: "official",
    name: "Official",
    type: "github",
    url: "https://github.com/LavX/bazarr-provider-catalog/blob/main/catalog.json",
    enabled: true,
    official: true,
    trusted: true,
  };

  function render(extra: Partial<ProviderHubCatalogSource>) {
    customRender(
      <SourcesDrawer
        opened
        onClose={() => undefined}
        sources={[{ ...source, ...extra }]}
        installedSourceUsage={{}}
      />,
    );
  }

  it("shows a Mirror badge and where the catalog was read from", async () => {
    render({
      last_origin: "pages",
      last_failure: { ...failure, retry_at: "2999-01-01T00:00:00Z" },
    });
    expect(await screen.findByText("Mirror")).toBeInTheDocument();
    expect(
      screen.getByText(/read from the GitHub Pages mirror/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/allows requests again at 2999-01-01 00:00:00 UTC/),
    ).toBeInTheDocument();
  });

  it("shows neither for a catalog read from GitHub", async () => {
    render({ last_origin: "raw", last_failure: null });
    await screen.findByText("Official");
    expect(screen.queryByText("Mirror")).toBeNull();
    expect(screen.queryByText(/GitHub Pages mirror/)).toBeNull();
  });

  it("shows neither for a source that never used the mirror", async () => {
    render({});
    await screen.findByText("Official");
    expect(screen.queryByText("Mirror")).toBeNull();
  });
});
