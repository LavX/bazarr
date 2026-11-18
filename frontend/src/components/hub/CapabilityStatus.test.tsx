import { describe, expect, it } from "vitest";
import type { ProviderHubCapability } from "@/apis/raw/providerHub";
import { CapabilityStatus } from "@/components/hub/CapabilityStatus";
import { customRender, screen, within } from "@/tests";

const ready: ProviderHubCapability = {
  capability: "flaresolverr.v1",
  source: "setting",
  ready: true,
  reason: "",
};
const missing: ProviderHubCapability = {
  capability: "host.captcha.v1",
  source: "none",
  ready: false,
  reason: "No captcha service is set up.",
};

describe("CapabilityStatus", () => {
  it("renders nothing for a plugin that declares no capability", () => {
    customRender(<CapabilityStatus capabilities={undefined} />);
    expect(screen.queryByRole("list")).toBeNull();
    customRender(<CapabilityStatus capabilities={[]} />);
    expect(screen.queryByRole("list")).toBeNull();
  });

  it("says ready in words, with no reason", () => {
    customRender(<CapabilityStatus capabilities={[ready]} />);
    const row = screen.getByText("FlareSolverr").closest("li");
    expect(row).not.toBeNull();
    expect(within(row as HTMLElement).getByText("Ready")).toBeVisible();
    expect(within(row as HTMLElement).queryByText(/set/)).toBeNull();
  });

  it("says not ready, with the reason, in words as well as colour", () => {
    customRender(<CapabilityStatus capabilities={[ready, missing]} />);
    const list = screen.getByRole("list", { name: "Host capabilities" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(2);
    const row = within(list).getByText("Captcha solver").closest("li");
    expect(within(row as HTMLElement).getByText("Not ready")).toBeVisible();
    expect(
      within(row as HTMLElement).getByText("No captcha service is set up."),
    ).toBeVisible();
  });

  it("shows an id the host has no name for as it is", () => {
    customRender(
      <CapabilityStatus
        capabilities={[{ ...ready, capability: "other.thing.v2" }]}
      />,
    );
    expect(screen.getByText("other.thing.v2")).toBeVisible();
  });
});
