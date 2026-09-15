/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import OverviewPanel from "@/pages/Statistics/OverviewPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

const badges = {
  episodes: 12,
  movies: 3,
  providers: 1,
  status: 0,
  sonarr_signalr: "LIVE",
  radarr_signalr: "DOWN",
  announcements: 0,
};

const status = {
  bazarr_version: "v2.6.1",
  database_engine: "PostgreSQL",
  database_migration: "abc123",
  operating_system: "Linux",
  python_version: "3.12.0",
  start_time: Math.floor(Date.now() / 1000) - 3661,
  timezone: "Europe/Budapest",
  cpu_cores: 8,
};

const mock = (opts: {
  health?: { object: string; issue: string }[];
  jobs?: { status: string }[];
}) => {
  server.use(
    http.get("/api/badges", () => HttpResponse.json(badges)),
    http.get("/api/system/status", () => HttpResponse.json({ data: status })),
    http.get("/api/system/health", () =>
      HttpResponse.json({ data: opts.health ?? [] }),
    ),
    http.get("/api/system/jobs", () =>
      HttpResponse.json({ data: opts.jobs ?? [] }),
    ),
  );
};

describe("Statistics > OverviewPanel", () => {
  beforeEach(() => {
    mock({});
  });

  it("shows the wanted-subtitle backlog for episodes and movies", async () => {
    customRender(<OverviewPanel />);

    expect(await screen.findByText("12")).toBeInTheDocument();
    expect(screen.getByText(/missing episode subtitles/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/missing movie subtitles/i)).toBeInTheDocument();
  });

  it("reports a healthy system when there are no health issues", async () => {
    customRender(<OverviewPanel />);

    expect(await screen.findByText(/no health issues/i)).toBeInTheDocument();
  });

  it("lists each health issue with the object it concerns", async () => {
    mock({
      health: [
        { object: "Sonarr", issue: "Sonarr is not reachable" },
        { object: "Profile: Default", issue: "Profile has no language" },
      ],
    });
    customRender(<OverviewPanel />);

    expect(
      await screen.findByText("Sonarr is not reachable"),
    ).toBeInTheDocument();
    expect(screen.getByText("Profile has no language")).toBeInTheDocument();
    expect(screen.getByText("Profile: Default")).toBeInTheDocument();
  });

  it("shows the per-kind SignalR feed state reported by badges", async () => {
    customRender(<OverviewPanel />);

    // Await the value, not the label: labels render before badges resolve.
    expect(await screen.findByText("LIVE")).toBeInTheDocument();
    expect(screen.getByText("DOWN")).toBeInTheDocument();
    expect(screen.getByText(/sonarr feed/i)).toBeInTheDocument();
    expect(screen.getByText(/radarr feed/i)).toBeInTheDocument();
  });

  it("renders uptime as a day and clock breakdown", async () => {
    customRender(<OverviewPanel />);

    expect(
      await screen.findByText(/\d+d \d{2}:\d{2}:\d{2}/),
    ).toBeInTheDocument();
  });

  it("shows the database engine so a Postgres install is identifiable", async () => {
    customRender(<OverviewPanel />);

    expect(await screen.findByText("PostgreSQL")).toBeInTheDocument();
  });

  it("labels the completed job count as capped when the deque is saturated", async () => {
    // jobs_completed_queue is deque(maxlen=10): 10 means "at least 10".
    mock({ jobs: Array.from({ length: 10 }, () => ({ status: "completed" })) });
    customRender(<OverviewPanel />);

    expect(await screen.findByText("10+")).toBeInTheDocument();
  });

  it("shows an exact completed job count below the cap", async () => {
    mock({ jobs: [{ status: "completed" }, { status: "running" }] });
    customRender(<OverviewPanel />);

    expect(await screen.findByText(/jobs completed/i)).toBeInTheDocument();
    expect(screen.queryByText("10+")).not.toBeInTheDocument();
  });
});
