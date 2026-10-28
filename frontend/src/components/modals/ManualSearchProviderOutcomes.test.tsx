/* eslint-disable camelcase, testing-library/no-node-access */

import { useQuery } from "@tanstack/react-query";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { MovieSearchModal } from "@/components/modals/ManualSearchModal";
import { customRender, screen, within } from "@/tests";

const MOVIE: Item.Movie = {
  id: 42,
  radarrId: 42,
  title: "Provider Outcomes",
  path: "/movies/provider-outcomes.mkv",
  sceneName: "",
  profileId: 1,
  fanart: "",
  overview: "",
  imdbId: "tt0000042",
  alternativeTitles: [],
  poster: "",
  year: "2026",
  monitored: true,
  tags: [],
  audio_language: [],
  subtitles: [],
  missing_subtitles: [],
};

const SEARCH_RESPONSE = {
  data: [
    {
      provider: "found-provider",
      url: "https://example.test/subtitles/42",
      language: "en",
      forced: "False",
      hearing_impaired: "False",
      score: 93,
      orig_score: 93,
      score_without_hash: 88,
      release_info: ["ProviderOutcomes.Movie.1080p"],
      matches: ["title", "year"],
      dont_matches: [],
      subtitle: null,
      original_format: "False",
    },
  ] satisfies SearchResultType[],
  providers: [
    { name: "found-provider", status: "found", results: 1 },
    { name: "empty-provider", status: "empty", results: 0 },
    { name: "failed-provider", status: "failed", results: 0 },
  ],
};

// This double models the modal query boundary. The subtitle data remains an
// array, with provider outcomes beside it. The current queued transport only
// carries subtitle rows, so transporting the metadata is a separate change.
function useSearchResponse(id?: number) {
  const result = useQuery({
    queryKey: ["manual-search-provider-outcomes", id],
    queryFn: async () => (id === undefined ? [] : SEARCH_RESPONSE.data),
    enabled: id !== undefined,
    retry: false,
  });

  return {
    ...result,
    providers: id === undefined ? undefined : SEARCH_RESPONSE.providers,
  };
}

async function renderCompletedSearch() {
  const user = userEvent.setup();
  const context = {} as Parameters<typeof MovieSearchModal>[0]["context"];

  customRender(
    <MovieSearchModal
      id="provider-outcomes-modal"
      context={context}
      innerProps={{
        item: MOVIE,
        query: useSearchResponse,
        download: vi.fn().mockResolvedValue(undefined),
      }}
    />,
  );

  await user.click(screen.getByRole("button", { name: /^Search$/ }));

  const subtitleRow = await screen.findByRole("row", {
    name: /ProviderOutcomes\.Movie\.1080p/,
  });
  expect(within(subtitleRow).getByText("found-provider")).toBeVisible();
  expect(within(subtitleRow).getByText("93%")).toBeVisible();
}

function presentationOf(row: HTMLElement) {
  const clone = row.cloneNode(true) as HTMLElement;
  const originalElements = [row, ...Array.from(row.querySelectorAll("*"))];
  const clonedElements = [clone, ...Array.from(clone.querySelectorAll("*"))];

  for (const element of clonedElements) {
    for (const attribute of Array.from(element.attributes)) {
      if (
        attribute.name === "id" ||
        attribute.name === "class" ||
        attribute.name === "role" ||
        attribute.name === "title" ||
        attribute.name === "href" ||
        attribute.name.startsWith("data-") ||
        attribute.name.startsWith("aria-")
      ) {
        element.removeAttribute(attribute.name);
      }
    }

    // Provider names, status labels and counts cannot establish a visual
    // distinction. Keep element structure, inline styles and icons.
    for (const child of Array.from(element.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        child.textContent = "";
      }
    }
  }

  // Include computed paint so CSS driven by classes or data-status can
  // distinguish the outcomes after those attributes leave the cloned markup.
  const paint = originalElements.map((element) => {
    const style = window.getComputedStyle(element);
    return [
      "color",
      "background-color",
      "border-color",
      "font-weight",
      "font-style",
      "opacity",
      "fill",
      "stroke",
      "text-decoration",
      "box-shadow",
    ].map((property) => style.getPropertyValue(property));
  });

  return { markup: clone.outerHTML, paint };
}

describe("ManualSearchModal provider outcomes", () => {
  it("shows found, empty and failed outcomes in response order beside the subtitle results", async () => {
    await renderCompletedSearch();

    const rows = screen.queryAllByTestId("provider-outcome");
    expect(rows).toHaveLength(3);

    const expected = [
      { name: "found-provider", status: "found", label: "Results found" },
      { name: "empty-provider", status: "empty", label: "No results" },
      { name: "failed-provider", status: "failed", label: "Failed" },
    ];

    for (const [index, outcome] of expected.entries()) {
      const row = rows[index];
      expect(row).toHaveAttribute("data-status", outcome.status);
      expect(within(row).getByText(outcome.name)).toBeVisible();
      expect(within(row).getByText(outcome.label)).toBeVisible();
    }
  });

  it("gives failed and empty providers different presentation beyond their labels", async () => {
    await renderCompletedSearch();

    const rows = screen.queryAllByTestId("provider-outcome");
    const empty = rows.find((row) => row.dataset.status === "empty");
    const failed = rows.find((row) => row.dataset.status === "failed");

    expect(empty, "The empty provider outcome is missing").toBeDefined();
    expect(failed, "The failed provider outcome is missing").toBeDefined();
    if (!empty || !failed) return;

    expect(presentationOf(failed)).not.toEqual(presentationOf(empty));
  });
});
