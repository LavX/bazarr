/* eslint-disable camelcase */
/**
 * Tests for the ItemEditForm component.
 *
 * Focus: the profile load-gate. When language profiles are not yet fetched the
 * component renders a Loader and no form elements. Once profiles are available
 * it renders the form and pre-selects the item's current profile in the
 * Languages Profile selector.
 *
 * Strategy: vi.mock controls the useLanguageProfiles hook synchronously so we
 * can exercise both the loading (data === undefined) and loaded states without
 * async race conditions. This mirrors how the real gate logic works: the
 * component checks data === undefined, not isFetching, to decide whether to
 * render the form body.
 */

import { describe, expect, it, vi } from "vitest";
import ItemEditForm from "@/components/forms/ItemEditForm";
import { customRender, screen, waitFor } from "@/tests";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const PROFILES: Language.Profile[] = [
  {
    profileId: 1,
    name: "English Only",
    items: [],
    mustContain: [],
    mustNotContain: [],
    originalFormat: false,
    cutoff: null,
    tag: undefined,
  },
  {
    profileId: 2,
    name: "English + French",
    items: [],
    mustContain: [],
    mustNotContain: [],
    originalFormat: false,
    cutoff: null,
    tag: undefined,
  },
];

/** Minimal Item.Movie satisfying Item.Base + MovieIdType. */
function makeMovie(profileId: number | null): Item.Movie {
  return {
    id: 10,
    radarrId: 10,
    title: "Test Movie",
    path: "/movies/test.mkv",
    profileId,
    fanart: "",
    overview: "",
    imdbId: "tt0000001",
    alternativeTitles: [],
    poster: "",
    year: "2024",
    monitored: true,
    tags: [],
    audio_language: [
      { code2: "en", name: "English", hi: false, forced: false },
    ],
    subtitles: [],
    missing_subtitles: [],
  };
}

/**
 * Stub UseMutationResult. We only need isPending and mutate for the form to
 * render; the rest are typed but never called in these tests.
 */
function makeMutation() {
  return {
    isPending: false,
    mutate: vi.fn(),
    mutateAsync: vi.fn(),
    reset: vi.fn(),
    isIdle: true as const,
    isSuccess: false as const,
    isError: false as const,
    isPaused: false as const,
    status: "idle" as const,
    error: null,
    data: undefined,
    variables: undefined,
    context: undefined,
    submittedAt: 0,
    failureCount: 0,
    failureReason: null,
  };
}

// ---------------------------------------------------------------------------
// Module mock: control useLanguageProfiles return value per test
// ---------------------------------------------------------------------------

vi.mock("@/apis/hooks", async (importActual) => {
  const actual = await importActual<typeof import("@/apis/hooks")>();
  return {
    ...actual,
    useLanguageProfiles: vi.fn(),
  };
});

// ---------------------------------------------------------------------------
// Test 1: Loader renders while profiles are not yet loaded
// ---------------------------------------------------------------------------

describe("ItemEditForm – load gate", () => {
  it("renders a Loader (and no form) while language profiles are still fetching", async () => {
    // Simulate data === undefined (in-flight, no cached result yet)
    const { useLanguageProfiles } = await import("@/apis/hooks");
    vi.mocked(useLanguageProfiles).mockReturnValue({
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      data: undefined as any,
      isFetching: true,
    } as ReturnType<typeof useLanguageProfiles>);

    customRender(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <ItemEditForm mutation={makeMutation() as any} item={makeMovie(1)} />,
    );

    // The gate blocks the form body. The Languages Profile combobox must NOT
    // be present.
    await waitFor(() => {
      expect(
        screen.queryByRole("combobox", { name: /languages profile/i }),
      ).not.toBeInTheDocument();
    });

    // The Cancel and Save buttons are part of the form body; they must also
    // be absent while loading.
    expect(
      screen.queryByRole("button", { name: /save/i }),
    ).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Test 2: Form renders and pre-selects the item's profile once loaded
  // -------------------------------------------------------------------------

  it("renders the Languages Profile selector pre-filled with the item's profile after profiles load", async () => {
    // Simulate successful load: data is the profiles array
    const { useLanguageProfiles } = await import("@/apis/hooks");
    vi.mocked(useLanguageProfiles).mockReturnValue({
      data: PROFILES,
      isFetching: false,
    } as ReturnType<typeof useLanguageProfiles>);

    // Item has profileId 2 → "English + French" should be preselected.
    customRender(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <ItemEditForm mutation={makeMutation() as any} item={makeMovie(2)} />,
    );

    // The form body must appear once profiles are available.
    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: /languages profile/i }),
      ).toBeInTheDocument();
    });

    // The Mantine Select renders the selected label as the input's value.
    expect(screen.getByDisplayValue("English + French")).toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Test 3: Null profileId leaves the selector blank
  // -------------------------------------------------------------------------

  it("renders the Languages Profile selector with no selection when item profileId is null", async () => {
    const { useLanguageProfiles } = await import("@/apis/hooks");
    vi.mocked(useLanguageProfiles).mockReturnValue({
      data: PROFILES,
      isFetching: false,
    } as ReturnType<typeof useLanguageProfiles>);

    customRender(
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      <ItemEditForm mutation={makeMutation() as any} item={makeMovie(null)} />,
    );

    await waitFor(() => {
      expect(
        screen.getByRole("combobox", { name: /languages profile/i }),
      ).toBeInTheDocument();
    });

    // No profile name should appear in the selector input value.
    expect(screen.queryByDisplayValue("English Only")).not.toBeInTheDocument();
    expect(
      screen.queryByDisplayValue("English + French"),
    ).not.toBeInTheDocument();
  });
});
