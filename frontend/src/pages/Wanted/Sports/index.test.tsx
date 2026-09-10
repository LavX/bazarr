/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import {
  sportarr,
  sportarrSibling,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import WantedSportsView from "@/pages/Wanted/Sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

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
    expect(posted).toEqual({ arr_instance_id: 42, language: "hu" }),
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
