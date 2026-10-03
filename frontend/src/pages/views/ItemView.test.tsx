/* eslint-disable camelcase */
import { useState } from "react";
import { ColumnDef } from "@tanstack/react-table";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { UsePaginationQueryResult } from "@/apis/queries/hooks";
import ItemView from "@/pages/views/ItemView";
import { customRender, screen, waitFor } from "@/tests";
import server from "@/tests/mocks/node";

// Sports is the one library that reads a page until a filter starts and then
// switches to a query for every row. Until that answers, the query client
// hands back the page it had, marked as placeholder data.
type Phase = "page" | "placeholder" | "settled";

const leagues: Item.Base[] = Array.from({ length: 60 }, (_, index) => ({
  title:
    index === 3 ? "Northern Cup" : index === 55 ? "Southern Cup" : `L${index}`,
  path: `/sports/${index}`,
  profileId: null,
  fanart: "",
  overview: "",
  imdbId: "",
  alternativeTitles: [],
  poster: "",
  year: "",
  monitored: true,
  tags: [],
  audio_language: [],
}));

const columns: ColumnDef<Item.Base>[] = [
  { header: "Name", accessorKey: "title" },
];

function queryFor(phase: Phase) {
  const fetchAll = phase !== "page";
  const rows = phase === "settled" ? leagues : leagues.slice(0, 25);
  return {
    data: { data: rows, total: leagues.length },
    isPlaceholderData: phase === "placeholder",
    paginationStatus: {
      isPageLoading: false,
      totalCount: leagues.length,
      pageSize: 25,
      pageCount: 3,
      page: 0,
      fetchAll,
    },
    controls: { gotoPage: () => undefined, setPageSize: () => undefined },
  } as unknown as UsePaginationQueryResult<Item.Base>;
}

// The buttons stand in for the Sports page: typing starts the filter, and
// the query for every row answers later.
function Library() {
  const [phase, setPhase] = useState<Phase>("page");
  return (
    <>
      <button type="button" onClick={() => setPhase("placeholder")}>
        Start filtering
      </button>
      <button type="button" onClick={() => setPhase("settled")}>
        Every row arrives
      </button>
      <ItemView
        query={queryFor(phase)}
        columns={columns}
        searchValue={phase === "page" ? "" : "cup"}
        onSearchChange={() => undefined}
        itemNoun={{ one: "league", other: "leagues" }}
      />
    </>
  );
}

// The status region repeats the count for screen readers; the count on
// screen is the other element holding the same words.
const onScreen = { ignore: "script, style, [role=status]" };

describe("ItemView count", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/system/languages/audio", () => HttpResponse.json([])),
    );
  });

  // While every row is still on its way the count says nothing, and an empty
  // count gave its width to the search field on a phone, which took it back
  // once the count returned. The label falls back to the total, the same
  // words the band has shown since its first load, so the width never
  // changes hands and no filtered number appears before it is known.
  it("keeps the total on the band while placeholder rows stand in", async () => {
    const user = userEvent.setup();
    customRender(<Library />);
    const count = await screen.findByText("60 leagues", onScreen);
    expect(count).toBeVisible();
    expect(count).toHaveAttribute("aria-hidden", "true");

    await user.click(screen.getByRole("button", { name: "Start filtering" }));

    // The same words as the first load, still on screen: the total is known
    // all along, so the band holds its width without inventing a number.
    expect(screen.getByText("60 leagues", onScreen)).toBe(count);
    expect(count).toBeVisible();
    expect(screen.queryByText(/ of 60 leagues$/)).toBeNull();
    // The status region says nothing until the real count arrives, so no
    // total is read out mid-filter either.
    await waitFor(() =>
      expect(screen.getByRole("status")).toBeEmptyDOMElement(),
    );

    await user.click(screen.getByRole("button", { name: "Every row arrives" }));

    const filtered = screen.getByText("2 of 60 leagues", onScreen);
    expect(filtered).toBeVisible();
    expect(screen.queryByText("60 leagues", onScreen)).toBeNull();
  });
});
