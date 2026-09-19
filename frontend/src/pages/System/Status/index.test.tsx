/* eslint-disable camelcase */
import { http } from "msw";
import { HttpResponse } from "msw";
import { expect, it } from "vitest";
import { sportarr } from "@/pages/Settings/Connections/__tests__/fixtures";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";
import SystemStatusView from ".";

function statusWith(sportsOn: boolean) {
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: {
          sonarr_version: "4.0.0",
          radarr_version: "5.0.0",
          sportarr_version: "4.1.6",
        },
      }),
    ),
    http.get("/api/system/health", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/settings", () =>
      HttpResponse.json({ general: { use_sportarr: sportsOn } }),
    ),
    http.get("/api/system/arr-instances", () =>
      HttpResponse.json(sportsOn ? [sportarr] : []),
    ),
  );
}

describe("System Status", () => {
  it("should render with status", async () => {
    server.use(
      http.get("/api/system/status", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );

    server.use(
      http.get("/api/system/health", () => {
        return HttpResponse.json({
          data: [],
        });
      }),
    );

    customRender(<SystemStatusView />);

    // TODO: Assert
  });

  it("shows the Sportarr version once Sportarr is in use", async () => {
    // The endpoint has reported it since sports landed and this page never
    // rendered it, so there was no way to see which Sportarr Bazarr was
    // actually talking to.
    statusWith(true);
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Sportarr Version", undefined, {
        timeout: 8000,
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("4.1.6")).toBeInTheDocument();
  });

  it("leaves the row out of a two-media install", async () => {
    statusWith(false);
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Radarr Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sportarr Version")).toBeNull();
  });
});
