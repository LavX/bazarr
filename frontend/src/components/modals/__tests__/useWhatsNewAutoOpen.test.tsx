import { FunctionComponent } from "react";
import { beforeEach, describe, expect, it } from "vitest";
import { useWhatsNewAutoOpen } from "@/components/modals/useWhatsNewAutoOpen";
import { getWhatsNewSlides, latestWhatsNewVersion } from "@/data/whatsNew";
import { customRender, screen } from "@/tests";
import { markWhatsNewSeen } from "@/utilities/whatsNew";

const Harness: FunctionComponent<{ enabled: boolean }> = ({ enabled }) => {
  useWhatsNewAutoOpen(enabled);
  return null;
};

// Derive the first slide from the live data so bumping latestWhatsNewVersion
// never breaks these tests on the title string.
const firstSlideTitle = getWhatsNewSlides(latestWhatsNewVersion)[0].title;

describe("useWhatsNewAutoOpen", () => {
  beforeEach(() => localStorage.clear());

  it("does not auto-open while disabled (e.g. on the login screen)", async () => {
    customRender(<Harness enabled={false} />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(firstSlideTitle)).not.toBeInTheDocument();
  });

  it("auto-opens once enabled and the version is unseen", async () => {
    customRender(<Harness enabled />);
    expect(await screen.findByText(firstSlideTitle)).toBeInTheDocument();
  });

  // The core "don't nag on every login" guarantee: once the current version has
  // been seen, the wizard must not auto-open again. It only returns when
  // latestWhatsNewVersion changes (the next release).
  it("does not auto-open after the current version has been seen", async () => {
    markWhatsNewSeen(latestWhatsNewVersion);
    customRender(<Harness enabled />);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(screen.queryByText(firstSlideTitle)).not.toBeInTheDocument();
  });
});
