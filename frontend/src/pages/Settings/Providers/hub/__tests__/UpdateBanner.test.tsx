/* eslint-disable camelcase */

import { describe, expect, it } from "vitest";
import type { ProviderHubInstallation } from "@/apis/raw/providerHub";
import { UpdateBanner } from "@/pages/Settings/Providers/hub/UpdateBanner";
import { customRender, screen } from "@/tests";

const row = (extra: Partial<ProviderHubInstallation>) =>
  ({
    provider_id: "someprovider",
    name: "Some Provider",
    state: "active",
    active_version: "1.0.0",
    ...extra,
  }) as ProviderHubInstallation;

const render = (providers: ProviderHubInstallation[]) =>
  customRender(
    <UpdateBanner
      providers={providers}
      catalog={undefined}
      onSwitchToUpdates={() => undefined}
    />,
  );

describe("UpdateBanner restart prompt", () => {
  it("is not there for a plugin removed while Bazarr+ runs", () => {
    render([
      row({ state: "removed", needs_restart: false }),
      row({ provider_id: "other", needs_restart: false }),
    ]);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.queryByText(/restart/i)).not.toBeInTheDocument();
  });

  it("asks for a restart for a staged install or update, and says so", () => {
    render([
      row({ state: "staged", active_version: null, needs_restart: true }),
    ]);
    expect(
      screen.getByText(
        "1 Provider Hub change is staged. Restart Bazarr+ to apply it.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Staged installs and updates finish after a restart."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /Restart Bazarr now/ }),
    ).toBeInTheDocument();
  });

  it("keeps the restart for a removal that could not be stopped live, and says why", () => {
    render([row({ state: "removed", needs_restart: true })]);
    expect(
      screen.getByText(
        "This plugin could not be stopped while Bazarr+ runs, so its removal finishes after a restart.",
      ),
    ).toBeInTheDocument();
  });

  it("uses the plural for several removals that could not be stopped live", () => {
    render([
      row({ state: "removed", needs_restart: true }),
      row({ provider_id: "another", state: "removed", needs_restart: true }),
    ]);
    expect(
      screen.getByText(
        "2 Provider Hub changes are staged. Restart Bazarr+ to apply them.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "These plugins could not be stopped while Bazarr+ runs, so their removals finish after a restart.",
      ),
    ).toBeInTheDocument();
  });

  it.each([
    [1, "1 plugin"],
    [2, "2 plugins"],
  ])(
    "counts %s staged removals alongside staged installs and updates",
    (count, plugins) => {
      render([
        row({
          provider_id: "install",
          state: "staged",
          active_version: null,
          needs_restart: true,
        }),
        row({
          provider_id: "update",
          state: "staged",
          staged_version: "2.0.0",
          needs_restart: true,
        }),
        row({
          provider_id: "live-removal",
          state: "removed",
          needs_restart: false,
        }),
        ...Array.from({ length: count }, (_, i) =>
          row({
            provider_id: `removal-${i}`,
            state: "removed",
            needs_restart: true,
          }),
        ),
      ]);
      expect(
        screen.getByText(
          `${count + 2} Provider Hub changes are staged. Restart Bazarr+ to apply them.`,
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByText(
          `A restart applies staged installs and updates and finishes removing ${plugins} that could not be stopped while Bazarr+ runs.`,
        ),
      ).toBeInTheDocument();
    },
  );
});
