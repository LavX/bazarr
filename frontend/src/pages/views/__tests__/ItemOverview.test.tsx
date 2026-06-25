/* eslint-disable camelcase */

/**
 * Tests for ItemOverview component.
 *
 * Why: Verifies that the shared media overview card renders the item title and
 * each passed detail entry (icon + text), that a null item renders without
 * crashing, and that audio language badges and the file-path badge appear when
 * the item is present.
 *
 * What: Renders ItemOverview directly with controlled props and asserts DOM
 * content via Testing Library queries.
 *
 * Test: Run with `cd frontend && npx vitest run --reporter=verbose`.
 */

import { faCalendarAlt, faHardDrive } from "@fortawesome/free-solid-svg-icons";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import ItemOverview from "@/pages/views/ItemOverview";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// The component renders useLanguages() which calls /api/system/languages?history=false
// on every mount. Add a default handler for every test in this file.
beforeEach(() => {
  server.use(http.get("/api/system/languages", () => HttpResponse.json([])));
});

// ---------------------------------------------------------------------------
// Shared fixture
// ---------------------------------------------------------------------------

function makeItem(overrides?: Partial<Item.Base>): Item.Base {
  return {
    title: "Test Movie Title",
    path: "/media/test.mkv",
    profileId: null,
    fanart: "",
    overview: "A test movie overview text.",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    monitored: true,
    tags: [],
    audio_language: [],
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// Test 1: Title renders when item is provided
// ---------------------------------------------------------------------------

describe("ItemOverview, title rendering", () => {
  it("renders the item title in the heading area", async () => {
    customRender(<ItemOverview item={makeItem()} />);

    await waitFor(() => {
      expect(screen.getByText("Test Movie Title")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 2: Detail badges render their text
// ---------------------------------------------------------------------------

describe("ItemOverview, detail badge rendering", () => {
  it("renders each detail entry text as a badge", async () => {
    const details = [
      { icon: faHardDrive, text: "1.4 GB" },
      { icon: faCalendarAlt, text: "2024" },
    ];

    customRender(<ItemOverview item={makeItem()} details={details} />);

    await waitFor(() => {
      expect(screen.getByText("1.4 GB")).toBeInTheDocument();
    });
    expect(screen.getByText("2024")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test 3: File-path badge is rendered for the item's path
// ---------------------------------------------------------------------------

describe("ItemOverview, file-path badge", () => {
  it("renders the item file path as a badge", async () => {
    customRender(<ItemOverview item={makeItem()} />);

    await waitFor(() => {
      expect(screen.getByText("/media/test.mkv")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 4: Audio language badge appears when item has audio_language entries
// ---------------------------------------------------------------------------

describe("ItemOverview, audio language badges", () => {
  it("renders audio language badge for each audio track", async () => {
    const item = makeItem({
      audio_language: [
        { code2: "en", name: "English" },
        { code2: "fr", name: "French" },
      ],
    });

    customRender(<ItemOverview item={item} />);

    await waitFor(() => {
      expect(screen.getByText("English")).toBeInTheDocument();
    });
    expect(screen.getByText("French")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------
// Test 5: Tags badge joins tags with a pipe when tags are present
// ---------------------------------------------------------------------------

describe("ItemOverview, tags badge", () => {
  it("renders joined tags in a badge when item has tags", async () => {
    const item = makeItem({ tags: ["hd", "bluray"] });

    customRender(<ItemOverview item={item} />);

    await waitFor(() => {
      expect(screen.getByText("hd|bluray")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 6: null item renders without crashing (loading state)
// ---------------------------------------------------------------------------

describe("ItemOverview, null item", () => {
  it("renders without crashing when item is null", () => {
    // Must not throw — the component should render the shell with no content.
    expect(() => {
      customRender(<ItemOverview item={null} />);
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// Test 7: Language profile badge shows when profile matches item's profileId
// ---------------------------------------------------------------------------

describe("ItemOverview, language profile badge", () => {
  it("renders the language profile name when a matching profile is found", async () => {
    server.use(
      http.get("/api/system/languages/profiles", () =>
        HttpResponse.json([
          {
            name: "English (Best)",
            profileId: 7,
            cutoff: null,
            items: [],
            mustContain: [],
            mustNotContain: [],
            originalFormat: null,
            tag: undefined,
          },
        ]),
      ),
    );

    const item = makeItem({ profileId: 7 });
    customRender(<ItemOverview item={item} />);

    await waitFor(() => {
      expect(screen.getByText("English (Best)")).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 8: overview text appears in the card
// ---------------------------------------------------------------------------

describe("ItemOverview, overview text", () => {
  it("renders the item overview description text", async () => {
    const item = makeItem({ overview: "A gripping tale of adventure." });

    customRender(<ItemOverview item={item} />);

    await waitFor(() => {
      expect(
        screen.getByText("A gripping tale of adventure."),
      ).toBeInTheDocument();
    });
  });
});

// ---------------------------------------------------------------------------
// Test 9: monitored vs unmonitored icon uses different icon variant
// ---------------------------------------------------------------------------

describe("ItemOverview, monitored icon variant", () => {
  it("renders the solid bookmark icon (fas) for a monitored item", async () => {
    customRender(<ItemOverview item={makeItem({ monitored: true })} />);

    // Solid (fas) bookmark = monitored. FontAwesome renders the SVG with
    // data-prefix="fas" and data-icon="bookmark".
    await waitFor(() => {
      const svgs = document.querySelectorAll(
        'svg[data-prefix="fas"][data-icon="bookmark"]',
      );
      expect(svgs.length).toBeGreaterThanOrEqual(1);
    });
  });

  it("renders the regular bookmark icon (far) for an unmonitored item", async () => {
    customRender(<ItemOverview item={makeItem({ monitored: false })} />);

    // Regular (far) bookmark = unmonitored.
    await waitFor(() => {
      const svgs = document.querySelectorAll(
        'svg[data-prefix="far"][data-icon="bookmark"]',
      );
      expect(svgs.length).toBeGreaterThanOrEqual(1);
    });
  });
});
