import { FunctionComponent } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { useWhatsNewAutoOpen } from "@/components/modals/useWhatsNewAutoOpen";
import { customRender, screen, waitFor } from "@/tests";

const Harness: FunctionComponent<{ enabled: boolean }> = ({ enabled }) => {
  useWhatsNewAutoOpen(enabled);
  return null;
};

describe("useWhatsNewAutoOpen", () => {
  beforeEach(() => localStorage.clear());

  it("does not auto-open while disabled (e.g. on the login screen)", async () => {
    customRender(<Harness enabled={false} />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText("Distribution Hub")).not.toBeInTheDocument();
  });

  it("auto-opens once enabled and the version is unseen", async () => {
    customRender(<Harness enabled />);
    await waitFor(() =>
      expect(screen.getByText("Distribution Hub")).toBeInTheDocument(),
    );
  });
});
