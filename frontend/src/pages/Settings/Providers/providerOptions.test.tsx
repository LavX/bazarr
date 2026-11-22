/* eslint-disable camelcase */

import { renderHook } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import { useProviderOptions } from "@/pages/Settings/Providers";

const row = (extra: Partial<ProviderHubInstallation>) =>
  ({
    provider_id: "hubonly",
    name: "Hub Only",
    state: "active",
    active_version: "1.0.0",
    needs_restart: false,
    ...extra,
  }) as ProviderHubInstallation;

const keys = (providers: ProviderHubInstallation[]) =>
  renderHook(() => useProviderOptions(providers)).result.current.map(
    (option) => option.key,
  );

describe("provider options from the Hub", () => {
  it("lists a running plugin", () => {
    expect(keys([row({})])).toContain("hubonly");
  });

  it("drops a plugin removed while Bazarr+ runs, so its card does not linger", () => {
    expect(keys([row({ state: "removed" })])).not.toContain("hubonly");
  });

  it("keeps a plugin whose removal waits for a restart, since it still serves", () => {
    expect(keys([row({ state: "removed", needs_restart: true })])).toContain(
      "hubonly",
    );
  });
});
