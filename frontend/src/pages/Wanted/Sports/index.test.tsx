/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import { toSportsWantedRow } from "@/apis/hooks/sports";
import type { SportsEvent } from "@/apis/raw/sports";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import WantedSportsView from "@/pages/Wanted/Sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

/** Open a themed select by its input's placeholder and return its listbox,
 *  like the shared Discover helper: a click focuses without opening in jsdom,
 *  so the opener falls back to ArrowDown, and the dropdown is queried hidden
 *  because its transition never settles here. The WantedView filter labels are
 *  plain text siblings, not Mantine `label` props, so the placeholder is what
 *  identifies the control, and every select on the page renders its options
 *  eagerly, so the opened dropdown is the one the input's aria-controls
 *  points at. */
async function openSelect(
  actor: ReturnType<typeof userEvent.setup>,
  placeholder: string,
) {
  const input = await screen.findByPlaceholderText(placeholder);
  await actor.click(input);
  if (input.getAttribute("aria-expanded") !== "true")
    await actor.keyboard("{ArrowDown}");
  const controlled = input.getAttribute("aria-controls");
  const listboxes = await screen.findAllByRole("listbox", { hidden: true });
  return (
    listboxes.find((box) => box.getAttribute("id") === controlled) ??
    listboxes[0]
  );
}

function owners(enabled = true) {
  server.use(
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json([
        { ...sportarr, enabled },
        { ...sportarrSibling, enabled },
      ]),
    ),
    // Sports surfaces are gated on the master toggle as well as on an enabled
    // owner, so drive both from the same flag.
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: enabled } }),
    ),
  );
}

const event = {
  id: 11,
  arr_instance_id: 42,
  league_id: 7,
  title: "Final",
  missing_subtitles: ["en", "hu:hi"],
};

it("pages through the shared start/length contract instead of a page number", async () => {
  owners();
  let query: URLSearchParams | null = null;
  server.use(
    http.get("/api/sports/wanted", ({ request }) => {
      query = new URL(request.url).searchParams;
      return HttpResponse.json({ data: [event], total: 1 });
    }),
  );
  customRender(<WantedSportsView />);
  // Three queries settle before a row exists: the instance list, the settings
  // carrying the master toggle, and only then the wanted page. That chain
  // outruns findBy's 1s default when the whole suite is running.
  await screen.findByText("Final", undefined, { timeout: 8000 });
  // The old page sent its own page number and a fixed length of 100, so it
  // could not use the shared paginated table at all.
  await waitFor(() => expect(query).not.toBeNull());
  expect(query!.get("start")).toBe("0");
  expect(Number(query!.get("length"))).toBeGreaterThan(0);
});

it("downloads only the language whose badge was clicked", async () => {
  // Episodes and Movies download exactly the language whose badge was
  // clicked. Sports had to open a manual search modal instead, because the
  // event-wide /automatic action calls search_event with language=None and so
  // searches, and may download, every missing language on the event. That
  // endpoint takes a single language now.
  owners();
  let posted: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
    http.post("/api/sports/events/11/automatic", async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({ queued: true, job_id: 1, message: "queued" });
    }),
    http.post("/api/sports/events/11/search", () => {
      throw new Error("a badge click must not open a manual search");
    }),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );

  await userEvent.setup().click(within(row).getByText("hu:HI"));

  await waitFor(() =>
    expect(posted).toEqual({ arr_instance_id: 42, language: "hu:hi" }),
  );
});

it("keeps the variant suffix on the badge click for every modifier", async () => {
  // The automatic path matches the posted key against the profile's missing
  // list, and that list stores the variant ("hu:forced"). A bare code is
  // refused as "No eligible missing language" with downloads: 0, so the click
  // silently does nothing. The unmodified key must still go out unchanged.
  owners();
  const forced = {
    ...event,
    id: 12,
    missing_subtitles: ["en", "hu:forced"],
  };
  let posted: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [forced], total: 1 }),
    ),
    http.post("/api/sports/events/12/automatic", async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({ queued: true, job_id: 1, message: "queued" });
    }),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );

  await userEvent.setup().click(within(row).getByText("hu:Forced"));
  await waitFor(() =>
    expect(posted).toEqual({ arr_instance_id: 42, language: "hu:forced" }),
  );

  await userEvent.setup().click(within(row).getByText("en"));
  await waitFor(() =>
    expect(posted).toEqual({ arr_instance_id: 42, language: "en" }),
  );
});

it("still offers the manual search for one language on right-click", async () => {
  owners();
  let searched: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
    http.get("/api/system/languages", () =>
      HttpResponse.json([
        { code2: "en", code3: "eng", name: "English", enabled: true },
      ]),
    ),
    http.post("/api/sports/events/11/search", async ({ request }) => {
      searched = await request.json();
      return HttpResponse.json({ data: [] });
    }),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );

  await userEvent.setup().pointer({
    target: within(row).getByText("hu:HI"),
    keys: "[MouseRight]",
  });
  const dialog = within(await screen.findByRole("dialog"));
  await userEvent.setup().click(dialog.getByRole("button", { name: "Search" }));

  await waitFor(() =>
    expect(searched).toEqual({
      arr_instance_id: 42,
      language: "hu",
      hi: true,
      forced: false,
    }),
  );
});

it("splits a modified language key into a real language badge", async () => {
  owners();
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );
  // "hu:hi" is one opaque string in the sports API. The shared Language badge
  // takes a parsed language and renders its own modifier suffix, so the key has
  // to be split rather than printed as-is.
  expect(within(row).getByText("hu:HI")).toBeInTheDocument();
  expect(within(row).queryByText("hu:hi")).toBeNull();
});

it("does not request the wanted list when every owner is disabled", async () => {
  owners(false);
  let calls = 0;
  server.use(
    http.get("/api/sports/wanted", () => {
      calls++;
      return HttpResponse.json({ data: [], total: 0 });
    }),
  );
  customRender(<WantedSportsView />);
  expect(
    await screen.findByText(
      "Enable a Sportarr instance in Connections to view sports.",
    ),
  ).toBeInTheDocument();
  expect(calls).toBe(0);
});

it("offers a selection checkbox so the shared Mass Translate button can be used", async () => {
  // The page declared no selection column, but the shared WantedView still
  // renders its "Mass Translate (N)" button. Nothing could ever be selected,
  // so the button sat disabled forever and getWantedItem was unreachable.
  // Episodes and Movies both ship this column.
  owners();
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 1 }),
    ),
  );
  customRender(<WantedSportsView />);

  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );
  const checkbox = within(row).getByRole("checkbox");
  expect(checkbox).not.toBeChecked();

  await userEvent.click(checkbox);

  await waitFor(() => expect(checkbox).toBeChecked());
  expect(
    await screen.findByRole("button", { name: /Mass Translate \(1\)/ }),
  ).toBeEnabled();
});

it("scans the whole library per owner, not just the loaded page", async () => {
  // The wanted list is paginated: the page holds one server page, and Scan
  // All used to map exactly those rows into per-event scan-disk items, so an
  // event on the second page was never scanned. It now sends one
  // representative row per owner and the batch arm runs the owner's
  // whole-library rescan.
  owners();
  let posted: unknown;
  server.use(
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 3 }),
    ),
    http.post("/api/subtitles/batch", async ({ request }) => {
      posted = await request.json();
      return HttpResponse.json({
        queued: 2,
        skipped: 0,
        errors: [],
        job_id: 7,
      });
    }),
  );
  customRender(<WantedSportsView />);
  await screen.findByText("Final", undefined, { timeout: 8000 });

  await userEvent
    .setup()
    .click(await screen.findByRole("button", { name: "Scan All" }));

  await waitFor(() =>
    expect(posted).toEqual({
      action: "scan-disk",
      items: [
        { type: "sports", arr_instance_id: 42 },
        { type: "sports", arr_instance_id: 43 },
      ],
    }),
  );
});

it("renders the event audio languages in an Audio column", async () => {
  // The sports API sends audio track NAMES ("Hungarian") because the indexer
  // reads them off the file and the audio profile rules resolve by name. The
  // rows translate those to code2 and the column renders the catalogue names.
  owners();
  const withAudio = {
    ...event,
    missing_subtitles: ["de"],
    audio_language: ["Hungarian", "English"],
  };
  server.use(
    http.get("/api/system/languages/audio", () =>
      HttpResponse.json([
        { code2: "en", name: "English" },
        { code2: "hu", name: "Hungarian" },
      ]),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [withAudio], total: 1 }),
    ),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );

  expect(
    screen.getByRole("columnheader", { name: "Audio" }),
  ).toBeInTheDocument();
  // The missing badges render "de", so an exact match on the language names
  // can only come from the Audio column.
  expect(
    within(row).getByText("Hungarian", { exact: true }),
  ).toBeInTheDocument();
  expect(within(row).getByText("English", { exact: true })).toBeInTheDocument();
});

it("keeps an audio language the catalogue does not know", async () => {
  // An unmapped name must survive the name-to-code translation and the
  // column's code-to-name lookup, so it renders rather than vanishing.
  owners();
  const unknown = {
    ...event,
    missing_subtitles: ["de"],
    audio_language: ["Klingon"],
  };
  server.use(
    http.get("/api/system/languages/audio", () =>
      HttpResponse.json([{ code2: "en", name: "English" }]),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [unknown], total: 1 }),
    ),
  );
  customRender(<WantedSportsView />);
  const row = await screen.findByRole(
    "row",
    { name: /Final/ },
    { timeout: 8000 },
  );

  expect(within(row).getByText("Klingon", { exact: true })).toBeInTheDocument();
});

it("narrows the wanted list through the Include Audio Languages filter", async () => {
  // The wanted API stores names, the filter compares code2: selecting
  // "English" must match the event whose file carries the English track
  // through the name-to-code translation. Comparing names to codes, as it
  // used to, filtered every row out.
  owners();
  const finnish = {
    ...event,
    id: 11,
    title: "Final",
    missing_subtitles: ["de"],
    audio_language: ["Hungarian"],
  };
  const derby = {
    ...event,
    id: 12,
    title: "Derby",
    missing_subtitles: ["de"],
    audio_language: ["English"],
  };
  server.use(
    http.get("/api/system/languages/audio", () =>
      HttpResponse.json([
        { code2: "en", name: "English" },
        { code2: "hu", name: "Hungarian" },
      ]),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [finnish, derby], total: 2 }),
    ),
  );
  customRender(<WantedSportsView />);
  const actor = userEvent.setup();
  await screen.findByRole("row", { name: /Final/ }, { timeout: 8000 });

  await actor.click(
    await screen.findByRole("button", { name: "Toggle filters" }),
  );
  const listbox = await openSelect(actor, "Select languages to include...");
  await actor.click(
    within(listbox).getByRole("option", { name: "English", hidden: true }),
  );

  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /Final/ })).toBeNull(),
  );
  expect(screen.getByRole("row", { name: /Derby/ })).toBeInTheDocument();
});

it("lists a missing language that is absent from the loaded page", async () => {
  // The missing-language select used to offer only the languages found on the
  // currently loaded page. It must come from the library-wide audio catalogue
  // like the Series and Movies pages, so a language that only exists on a
  // later wanted page is still selectable.
  owners();
  server.use(
    http.get("/api/system/languages/audio", () =>
      HttpResponse.json([
        { code2: "en", name: "English" },
        { code2: "hu", name: "Hungarian" },
        { code2: "eu", name: "Basque" },
      ]),
    ),
    http.get("/api/sports/wanted", () =>
      HttpResponse.json({ data: [event], total: 2 }),
    ),
  );
  customRender(<WantedSportsView />);
  const actor = userEvent.setup();
  await screen.findByText("Final", undefined, { timeout: 8000 });

  await actor.click(
    await screen.findByRole("button", { name: "Toggle filters" }),
  );
  const listbox = await openSelect(actor, "Select a language...");

  expect(
    within(listbox).getByRole("option", { name: "Basque", hidden: true }),
  ).toBeInTheDocument();
});

it("maps audio track names to code2 through the language catalogue", () => {
  // The sports API sends names ("Hungarian"); the wanted rows must carry the
  // code2s the filters and the Audio column use. An unknown name survives so
  // no entry silently vanishes.
  const nameToCode = new Map([
    ["English", "en"],
    ["Hungarian", "hu"],
  ]);
  const row = toSportsWantedRow(
    {
      ...event,
      audio_language: ["Hungarian", "English", "Klingon"],
    } as SportsEvent,
    nameToCode,
  );
  expect(row.audio_language).toEqual(["hu", "en", "Klingon"]);
});

it("re-fetches wanted rows when the audio catalogue lands after them", async () => {
  // Cold cache: the wanted rows resolve before /system/languages/audio, so
  // toSportsWantedRow runs with an empty name map. The pagination key carries
  // the map signature, so when the catalogue lands the rows are re-fetched
  // through the real map instead of keeping unmapped names until the next
  // refetch, and a code2 filter comparison against them is never dead.
  owners();
  let wantedCalls = 0;
  const finnish = {
    ...event,
    id: 11,
    title: "Final",
    missing_subtitles: ["de"],
    audio_language: ["Hungarian"],
  };
  const derby = {
    ...event,
    id: 12,
    title: "Derby",
    missing_subtitles: ["de"],
    audio_language: ["English"],
  };
  server.use(
    http.get("/api/system/languages/audio", async () => {
      await new Promise((resolve) => setTimeout(resolve, 250));
      return HttpResponse.json([
        { code2: "en", name: "English" },
        { code2: "hu", name: "Hungarian" },
      ]);
    }),
    http.get("/api/sports/wanted", () => {
      wantedCalls++;
      return HttpResponse.json({ data: [finnish, derby], total: 2 });
    }),
  );
  customRender(<WantedSportsView />);
  const actor = userEvent.setup();
  await screen.findByRole("row", { name: /Final/ }, { timeout: 8000 });

  // The catalogue landing changes the map signature, so the initial
  // name-carrying rows are re-derived without any filter interaction.
  await waitFor(() => expect(wantedCalls).toBeGreaterThanOrEqual(2));

  await actor.click(
    await screen.findByRole("button", { name: "Toggle filters" }),
  );
  const listbox = await openSelect(actor, "Select languages to include...");
  await actor.click(
    within(listbox).getByRole("option", { name: "English", hidden: true }),
  );

  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /Final/ })).toBeNull(),
  );
  expect(screen.getByRole("row", { name: /Derby/ })).toBeInTheDocument();
});
