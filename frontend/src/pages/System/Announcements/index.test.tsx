import userEvent from "@testing-library/user-event";
import { http } from "msw";
import { HttpResponse } from "msw";
import { vi } from "vitest";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import SystemAnnouncementsView from ".";

const V230 =
  "Bazarr+ v2.3.0 is live! This release introduces the Provider Hub, a built-in marketplace for installable subtitle providers. First installable provider: SubtitleCat, the top-voted request on the upstream feature tracker (https://bazarr.featureupvote.com/suggestions/54294/provider-request-subtitle-cat).";

function serve(announcements: unknown[]) {
  server.use(
    http.get("/api/system/announcements", () =>
      HttpResponse.json({ data: announcements }),
    ),
  );
}

describe("System Announcements", () => {
  it("should render with empty announcements", async () => {
    serve([]);

    customRender(<SystemAnnouncementsView />);

    await waitFor(() => {
      expect(
        screen.getByText(/No announcements for now, come back later!/i),
      ).toBeInTheDocument();
    });
  });

  it("should render with announcements", async () => {
    const mockAnnouncements = [
      {
        text: "New Subtitle Provider!",
        dismissible: true,
      },
      {
        text: "Python Deprecated!",
        dismissible: false,
      },
    ];

    serve(mockAnnouncements);

    customRender(<SystemAnnouncementsView />);

    await waitFor(() => {
      expect(screen.getByText("New Subtitle Provider!")).toBeInTheDocument();
    });

    expect(screen.getByText("Python Deprecated!")).toBeInTheDocument();

    const dismissButtons = screen.getAllByLabelText("Dismiss announcement");

    // Each entry is its own card, named by its own headline, so the button a
    // reader can press is found by the entry it belongs to rather than by
    // walking the DOM up from the button.
    const dismissableButton = within(
      screen.getByRole("article", { name: "New Subtitle Provider!" }),
    ).getByLabelText("Dismiss announcement");

    const nonDismissableButton = within(
      screen.getByRole("article", { name: "Python Deprecated!" }),
    ).getByLabelText("Dismiss announcement");

    expect(dismissButtons).toHaveLength(2);
    expect(dismissableButton).not.toBeDisabled();
    expect(nonDismissableButton).toBeDisabled();
  });

  it("reads the lead sentence as a headline and keeps the whole entry", async () => {
    serve([
      {
        dismissible: true,
        hash: "hash-230",
        link: "https://github.com/LavX/bazarr/releases",
        text: V230,
        timestamp: "4 months ago",
      },
    ]);

    customRender(<SystemAnnouncementsView />);

    expect(
      await screen.findByRole("heading", { name: "Bazarr+ v2.3.0 is live!" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Release")).toBeInTheDocument();
    expect(screen.getByText("4 months ago")).toBeInTheDocument();
    // The body is the rest of the entry, not a summary of it.
    expect(
      screen.getByText(/This release introduces the Provider Hub/),
    ).toBeInTheDocument();
    // A URL the feed left mid-sentence stays reachable.
    expect(
      screen.getByRole("link", {
        name: "https://bazarr.featureupvote.com/suggestions/54294/provider-request-subtitle-cat",
      }),
    ).toHaveAttribute(
      "href",
      "https://bazarr.featureupvote.com/suggestions/54294/provider-request-subtitle-cat",
    );
    // The card's own source is named by its host rather than the word "Link".
    expect(
      screen.getByRole("link", { name: "Read more at github.com" }),
    ).toHaveAttribute("href", "https://github.com/LavX/bazarr/releases");
  });

  it("offers the disclosure for a body the card cannot show in full", async () => {
    // jsdom does no layout, so both measurements are 0 and a real overflow
    // would never be observed. The height is what the browser would report.
    const scrollHeight = vi
      .spyOn(Element.prototype, "scrollHeight", "get")
      .mockReturnValue(400);

    serve([
      {
        dismissible: true,
        hash: "hash-230",
        link: "",
        text: V230,
        timestamp: "4 months ago",
      },
    ]);

    const user = userEvent.setup();
    customRender(<SystemAnnouncementsView />);

    const toggle = await screen.findByRole("button", { name: "Show more" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    // Clamping is visual only: the tail of the entry is on the page from the
    // start, so a reader who expands it never loses the end of the sentence.
    expect(
      screen.getByText(/Browse the marketplace|featureupvote/),
    ).toBeInTheDocument();

    await user.click(toggle);

    expect(screen.getByRole("button", { name: "Show less" })).toHaveAttribute(
      "aria-expanded",
      "true",
    );

    scrollHeight.mockRestore();
  });

  it("dismisses an announcement by its hash", async () => {
    const bodies: string[] = [];
    serve([
      {
        dismissible: true,
        hash: "hash-230",
        link: "",
        text: V230,
        timestamp: "4 months ago",
      },
    ]);
    server.use(
      http.post("/api/system/announcements", async ({ request }) => {
        bodies.push(await request.text());
        return new HttpResponse(null, { status: 204 });
      }),
    );

    const user = userEvent.setup();
    customRender(<SystemAnnouncementsView />);

    await user.click(await screen.findByLabelText("Dismiss announcement"));

    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toContain("hash-230");
  });
});
