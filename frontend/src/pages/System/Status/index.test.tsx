/* eslint-disable camelcase */
import { http } from "msw";
import { HttpResponse } from "msw";
import { expect, it } from "vitest";
import {
  radarrDefault,
  sonarrDefault,
  sportarr,
} from "@/pages/Settings/Connections/__tests__/fixtures";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";
import SystemStatusView from ".";

type Connected = {
  sonarr?: boolean;
  radarr?: boolean;
  sports?: boolean;
  mediaServers?: System.MediaServerStatus[];
};

function statusWith({
  sonarr = false,
  radarr = false,
  sports = false,
  mediaServers,
}: Connected) {
  const instances = [
    ...(sonarr ? [sonarrDefault] : []),
    ...(radarr ? [radarrDefault] : []),
    ...(sports ? [sportarr] : []),
  ];
  server.use(
    http.get("/api/system/status", () =>
      HttpResponse.json({
        data: {
          sonarr_version: sonarr ? "4.0.0" : "",
          radarr_version: radarr ? "5.0.0" : "",
          sportarr_version: sports ? "4.1.6" : "",
          ...(mediaServers ? { media_servers: mediaServers } : {}),
        },
      }),
    ),
    http.get("/api/system/health", () => HttpResponse.json({ data: [] })),
    http.get("/api/system/settings", () =>
      HttpResponse.json({
        general: {
          use_sonarr: sonarr,
          use_radarr: radarr,
          use_sportarr: sports,
        },
      }),
    ),
    http.get("/api/system/arr-instances", () => HttpResponse.json(instances)),
  );
}

function mediaServer(
  overrides: Partial<System.MediaServerStatus> = {},
): System.MediaServerStatus {
  return {
    id: "3f1d0d2e-0000-4000-8000-000000000001",
    kind: "emby",
    name: "Attic",
    state: "connected",
    version: "4.8.11.0",
    ...overrides,
  };
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

  it("shows the Sonarr and Radarr versions once they are in use", async () => {
    statusWith({ sonarr: true, radarr: true });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Sonarr Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("4.0.0")).toBeInTheDocument();
    expect(await screen.findByText("Radarr Version")).toBeInTheDocument();
    expect(await screen.findByText("5.0.0")).toBeInTheDocument();
  });

  it("leaves out the rows for arrs that are not configured", async () => {
    // A blank row reads as broken rather than as absent, which is exactly what
    // a fresh install with nothing connected used to show.
    statusWith({});
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Bazarr Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sonarr Version")).toBeNull();
    expect(screen.queryByText("Radarr Version")).toBeNull();
    expect(screen.queryByText("Sportarr Version")).toBeNull();
  });

  it("shows the Sportarr version once Sportarr is in use", async () => {
    // The endpoint has reported it since sports landed and this page never
    // rendered it, so there was no way to see which Sportarr Bazarr was
    // actually talking to.
    statusWith({ sports: true });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Sportarr Version", undefined, {
        timeout: 8000,
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("4.1.6")).toBeInTheDocument();
  });

  it("leaves the row out of a two-media install", async () => {
    statusWith({ radarr: true, sports: false });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Radarr Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Sportarr Version")).toBeNull();
  });

  it("shows a connected media server's version", async () => {
    statusWith({ mediaServers: [mediaServer()] });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Emby Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("4.8.11.0")).toBeInTheDocument();
  });

  it("names each instance when one product is connected twice", async () => {
    statusWith({
      mediaServers: [
        mediaServer({ name: "Attic" }),
        mediaServer({
          id: "3f1d0d2e-0000-4000-8000-000000000002",
          name: "Basement",
          version: "4.9.0.0",
        }),
      ],
    });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Emby Version (Attic)", undefined, {
        timeout: 8000,
      }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Emby Version (Basement)")).toBeVisible();
    expect(await screen.findByText("4.8.11.0")).toBeInTheDocument();
    expect(await screen.findByText("4.9.0.0")).toBeInTheDocument();
    expect(screen.queryByText("Emby Version")).toBeNull();
  });

  it("does not claim a version for a server it cannot reach", async () => {
    statusWith({
      mediaServers: [
        mediaServer({ kind: "jellyfin", state: "unreachable", version: "" }),
      ],
    });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Jellyfin Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(await screen.findByText("Unreachable")).toBeInTheDocument();
  });

  it("says so when a connected server reports no version", async () => {
    // Silo's health endpoint carries an id and a name and nothing else.
    statusWith({
      mediaServers: [mediaServer({ kind: "silo", version: "" })],
    });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Silo Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(
      await screen.findByText("Connected, no version reported"),
    ).toBeInTheDocument();
  });

  it("shows nothing for a destination the endpoint leaves out", async () => {
    // Disabled destinations, and destinations whose product is switched off,
    // never reach the payload: the endpoint is the gate, so the page has no
    // row to render and no version to claim.
    statusWith({ mediaServers: [] });
    customRender(<SystemStatusView />);

    expect(
      await screen.findByText("Bazarr Version", undefined, { timeout: 8000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Emby Version")).toBeNull();
    expect(screen.queryByText("Plex Version")).toBeNull();
    expect(screen.queryByText("Silo Version")).toBeNull();
    expect(screen.queryByText("Jellyfin Version")).toBeNull();
  });
});
