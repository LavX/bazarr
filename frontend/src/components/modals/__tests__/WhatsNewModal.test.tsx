import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { WhatsNewView } from "@/components/modals/WhatsNewModal";
import type { WhatsNewSlide } from "@/data/whatsNew";
import { customRender, screen, waitFor } from "@/tests";

const SLIDES: WhatsNewSlide[] = [
  { title: "Combined subtitles", body: "Merge multiple SRTs into one track." },
  {
    title: "Provider Hub opt-in",
    body: "Startup auto-install is now off by default.",
    cta: { label: "Open Settings", to: "/settings/general" },
  },
];

describe("WhatsNewView wizard", () => {
  it("starts on the first slide with Back disabled", () => {
    customRender(<WhatsNewView version="2.4.0" slides={SLIDES} />);

    expect(screen.getByText("Combined subtitles")).toBeInTheDocument();
    expect(screen.queryByText("Provider Hub opt-in")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /back/i })).toBeDisabled();
  });

  it("renders one progress dot per slide", () => {
    customRender(<WhatsNewView version="2.4.0" slides={SLIDES} />);
    expect(
      screen.getAllByRole("button", { name: /go to update/i }),
    ).toHaveLength(SLIDES.length);
  });

  it("advances with Next and goes back with Back", async () => {
    const user = userEvent.setup();
    customRender(<WhatsNewView version="2.4.0" slides={SLIDES} />);

    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(screen.getByText("Provider Hub opt-in")).toBeInTheDocument();
    expect(screen.queryByText("Combined subtitles")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /back/i }));
    expect(screen.getByText("Combined subtitles")).toBeInTheDocument();
  });

  it("shows a finish button (not Next) on the last slide", async () => {
    const user = userEvent.setup();
    customRender(<WhatsNewView version="2.4.0" slides={SLIDES} />);

    await user.click(screen.getByRole("button", { name: /next/i }));

    expect(
      screen.queryByRole("button", { name: /next/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /got it/i })).toBeInTheDocument();
  });

  it("renders the slide CTA when present", async () => {
    const user = userEvent.setup();
    customRender(<WhatsNewView version="2.4.0" slides={SLIDES} />);

    expect(
      screen.queryByRole("button", { name: "Open Settings" }),
    ).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /next/i }));
    expect(
      screen.getByRole("button", { name: "Open Settings" }),
    ).toBeInTheDocument();
  });

  it("invokes onNavigate with the CTA target when the CTA is clicked", async () => {
    const user = userEvent.setup();
    const onNavigate = vi.fn();
    customRender(
      <WhatsNewView version="2.4.0" slides={SLIDES} onNavigate={onNavigate} />,
    );

    await user.click(screen.getByRole("button", { name: /next/i }));
    await user.click(screen.getByRole("button", { name: "Open Settings" }));
    await waitFor(() =>
      expect(onNavigate).toHaveBeenCalledWith("/settings/general"),
    );
  });
});
