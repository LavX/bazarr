/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { expect, it } from "vitest";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import SportsHistoryView from "@/pages/History/Sports";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import WantedSportsView from "@/pages/Wanted/Sports";
import { customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";
import { openSelect } from "@/tests/select";

const availability = { browsable: true, searchable: true, retained: true };
const arr = {
  id: 7,
  arr_instance_id: 42,
  league_id: 17,
  title: "Arr Final",
  missing_subtitles: ["en"],
  language: "en",
  action: 1,
};
const source = {
  ...arr,
  origin: "source",
  arr_instance_id: null,
  league_id: null,
  source_id: 8,
  source_item_id: 70,
  source_group_id: 17,
  source_name: "Local archive",
  source_key: "source:8",
  source_kind: "folder",
  identity_state: "parsed",
  title: "Source Final",
  path: "/media/source.mkv",
  missing_subtitles: [
    { code2: "en", name: "English", hi: true, forced: false },
  ],
};

const pages = [
  ["wanted", WantedSportsView],
  ["history", SportsHistoryView],
  ["blacklist", BlacklistSportsView],
] as const;

it.each(pages)(
  "filters the mixed %s activity by source key",
  async (route, Page) => {
    window.history.replaceState(null, "", "/");
    const user = userEvent.setup();
    let query = new URLSearchParams();
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
      http.get("/api/system/status", () => HttpResponse.json({ data: {} })),
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr]),
      ),
      http.get("/api/system/media-sources", () =>
        HttpResponse.json({
          data: [
            {
              id: 8,
              name: "Local archive",
              kind: "folder",
              enabled: true,
              libraries: [{ id: 9, enabled: true, content_kind: "sports" }],
            },
          ],
          availability: {
            movie: availability,
            series: availability,
            sports: availability,
          },
        }),
      ),
      http.get(`/api/sports/${route}`, ({ request }) => {
        query = new URL(request.url).searchParams;
        const data =
          query.get("source_key[]") === "source:8" ? [source] : [arr, source];
        return HttpResponse.json({ data, total: data.length });
      }),
    );
    customRender(<Page />);
    expect(
      await screen.findByRole(
        "link",
        { name: "Source Final" },
        { timeout: 8000 },
      ),
    ).toHaveAttribute("href", "/sports/source/17");
    expect(screen.getByRole("link", { name: "Arr Final" })).toHaveAttribute(
      "href",
      "/sports/17?instance=42",
    );
    expect(
      within(screen.getByRole("row", { name: /Source Final/ })).getByText(
        "Local archive",
      ),
    ).toBeVisible();
    if (route === "wanted") expect(screen.getByText("en:HI")).toBeVisible();
    const listbox = await openSelect(user, "All sources");
    await user.click(
      within(listbox).getByRole("option", { name: "Local archive" }),
    );
    await waitFor(() => {
      expect(query.getAll("source_key[]")).toEqual(["source:8"]);
      expect(screen.queryByRole("link", { name: "Arr Final" })).toBeNull();
    });
    expect(query.get("sources")).toBe("1");
    expect(query.has("arr_instance_id")).toBe(false);
  },
);
