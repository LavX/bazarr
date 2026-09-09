/* eslint-disable camelcase */
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import queryClient from "@/apis/queries";
import { QueryKeys } from "@/apis/queries/keys";
import {
  sportarr,
  sportarrSibling,
  sportsProfile,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import Sports from "@/pages/Sports";
import { act, customRender, screen, waitFor, within } from "@/tests";
import server from "@/tests/mocks/node";

describe("sports library", () => {
  beforeEach(() => {
    // Every sports surface is gated on the master toggle now.
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({ general: { use_sportarr: true } }),
      ),
    );
  });
  it("renders local-ID links, owner, art and counts and assigns a profile", async () => {
    const user = userEvent.setup();
    let assigned: unknown;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([sportarr, sportarrSibling]),
      ),
      http.get("/api/system/languages/profiles", () =>
        HttpResponse.json([sportsProfile]),
      ),
      http.get("/api/sports/leagues", () =>
        HttpResponse.json({
          data: [
            {
              id: 51,
              arr_instance_id: 42,
              sportarrLeagueId: 7,
              title: "Premier League",
              sport: "Football",
              poster: "https://images.example/poster.jpg",
              eventCount: 3,
              eventFileCount: 4,
              profileId: assigned ? 3 : null,
            },
          ],
          total: 1,
        }),
      ),
      http.patch("/api/sports/leagues/51", async ({ request }) => {
        assigned = await request.json();
        return new HttpResponse(null, { status: 204 });
      }),
    );
    customRender(<Sports />);
    expect(
      await screen.findByRole("link", { name: "Premier League" }),
    ).toHaveAttribute("href", "/sports/51?instance=42");
    expect(
      screen.getByRole("img", { name: "Premier League poster" }),
    ).toHaveAttribute("src", "https://images.example/poster.jpg");
    expect(screen.getByText("Football")).toBeInTheDocument();
    expect(screen.getByText("3 events · 4 files")).toBeInTheDocument();
    expect(
      within(
        screen.getByRole("region", { name: "Premier League league" }),
      ).getByText("Main Sportarr"),
    ).toBeInTheDocument();
    await screen.findByRole("option", { name: "English Sports" });
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Profile for Premier League" }),
      "3",
    );
    await waitFor(() =>
      expect(assigned).toEqual({ arr_instance_id: 42, profileId: 3 }),
    );
  });
  it("does not request the library with no enabled Sportarr", async () => {
    let requests = 0;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled: false }]),
      ),
      http.get("/api/sports/leagues", () => {
        requests++;
        return HttpResponse.json({ data: [], total: 0 });
      }),
    );
    customRender(<Sports />);
    expect(
      await screen.findByText(
        "Enable a Sportarr instance in Connections to view sports.",
      ),
    ).toBeInTheDocument();
    expect(requests).toBe(0);
  });
  it("drops a disabled owner's cached leagues while another owner stays enabled", async () => {
    let enabled = true;
    server.use(
      http.get("/api/system/arr-instances", () =>
        HttpResponse.json([{ ...sportarr, enabled }, sportarrSibling]),
      ),
      http.get("/api/sports/leagues", () =>
        HttpResponse.json({
          data: enabled
            ? [
                {
                  id: 51,
                  arr_instance_id: 42,
                  sportarrLeagueId: 7,
                  title: "Disabled owner league",
                  sport: "Football",
                  eventCount: 0,
                  eventFileCount: 0,
                  profileId: null,
                },
              ]
            : [],
          total: enabled ? 1 : 0,
        }),
      ),
    );
    customRender(<Sports />);
    expect(
      await screen.findByRole("link", { name: "Disabled owner league" }),
    ).toBeInTheDocument();
    enabled = false;
    await act(async () => {
      await queryClient.invalidateQueries({
        queryKey: [QueryKeys.ArrInstances],
      });
    });
    await waitFor(() =>
      expect(
        screen.queryByRole("link", { name: "Disabled owner league" }),
      ).toBeNull(),
    );
  });
});
