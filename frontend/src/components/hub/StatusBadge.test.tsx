import { describe, expect, it } from "vitest";
import { ProviderStatusBadge } from "@/components/hub/StatusBadge";
import { customRender, screen } from "@/tests";

describe("ProviderStatusBadge", () => {
  it("says Removing for a plugin being removed while Bazarr+ runs, even when it needed repair", () => {
    customRender(
      <ProviderStatusBadge
        state="removed"
        pendingRestart={false}
        activeVersion="1.0.0"
        needsRepair
      />,
    );
    expect(screen.getByText("Removing")).toBeVisible();
    expect(screen.queryByText("Needs repair")).not.toBeInTheDocument();
  });

  it("still says the removal is staged when it waits for a restart", () => {
    customRender(
      <ProviderStatusBadge
        state="removed"
        pendingRestart
        activeVersion="1.0.0"
      />,
    );
    expect(screen.getByText("Removal staged")).toBeVisible();
  });
});
