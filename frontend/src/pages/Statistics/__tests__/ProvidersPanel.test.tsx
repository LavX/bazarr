/* eslint-disable camelcase */
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import type { ProviderHubJob } from "@/apis/raw/providerHub";
import ProvidersPanel from "@/pages/Statistics/ProvidersPanel";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

interface MockOpts {
  providers?: System.Provider[];
  installs?: Record<string, unknown>[];
  jobs?: ProviderHubJob[];
  sources?: Record<string, unknown>[];
}

const mock = (opts: MockOpts = {}) => {
  server.use(
    http.get("/api/providers", () =>
      HttpResponse.json({ data: opts.providers ?? [] }),
    ),
    http.get("/api/provider-hub/providers", () =>
      HttpResponse.json({ data: opts.installs ?? [] }),
    ),
    http.get("/api/provider-hub/jobs", () =>
      HttpResponse.json({ data: opts.jobs ?? [] }),
    ),
    http.get("/api/provider-hub/catalog", () =>
      HttpResponse.json({ sources: opts.sources ?? [], entries: [] }),
    ),
  );
};

describe("Statistics > ProvidersPanel", () => {
  beforeEach(() => mock());

  it("counts healthy and throttled providers", async () => {
    mock({
      providers: [
        { name: "a", status: "Good", retry: "-" },
        { name: "b", status: "Good", retry: "-" },
        { name: "c", status: "DownloadLimitExceeded", retry: "in 4 hours" },
      ],
    });
    customRender(<ProvidersPanel />);

    // Await the value: labels render before the providers query resolves.
    expect(await screen.findByText("2 / 3")).toBeInTheDocument();
    expect(screen.getByText(/providers healthy/i)).toBeInTheDocument();
  });

  it("lists a throttled provider with its reason and retry time", async () => {
    mock({
      providers: [
        {
          name: "opensubtitles",
          status: "DownloadLimitExceeded",
          retry: "in 4 hours",
        },
      ],
    });
    customRender(<ProvidersPanel />);

    expect(await screen.findByText("opensubtitles")).toBeInTheDocument();
    expect(screen.getByText("DownloadLimitExceeded")).toBeInTheDocument();
    expect(screen.getByText("in 4 hours")).toBeInTheDocument();
  });

  it("says so plainly when no provider is throttled", async () => {
    mock({ providers: [{ name: "a", status: "Good", retry: "-" }] });
    customRender(<ProvidersPanel />);

    expect(
      await screen.findByText(/no providers are throttled/i),
    ).toBeInTheDocument();
  });

  it("warns that throttle history is not retained", async () => {
    // Expired throttles are deleted from throttled_providers.dat, so this view
    // is a snapshot and must not be read as a rate.
    customRender(<ProvidersPanel />);

    expect(await screen.findByText(/current state only/i)).toBeInTheDocument();
  });

  it("breaks down installed Provider Hub plugins by state", async () => {
    mock({
      installs: [
        { provider_id: "p1", state: "active" },
        { provider_id: "p2", state: "active" },
        { provider_id: "p3", state: "staged", pending_restart: true },
      ],
    });
    customRender(<ProvidersPanel />);

    expect(await screen.findByText(/plugins installed/i)).toBeInTheDocument();
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/1 pending restart/i)).toBeInTheDocument();
  });

  it("reports install duration percentiles from the hub job log", async () => {
    mock({
      jobs: [
        { action: "install", state: "completed", duration_ms: 1000 },
        { action: "install", state: "completed", duration_ms: 3000 },
      ],
    });
    customRender(<ProvidersPanel />);

    expect(await screen.findByText("2.0s")).toBeInTheDocument();
    expect(screen.getByText(/median job duration/i)).toBeInTheDocument();
  });

  it("shows a dash when no hub job recorded a duration", async () => {
    mock({ jobs: [{ action: "install", state: "running" }] });
    customRender(<ProvidersPanel />);

    expect(await screen.findByText(/median job duration/i)).toBeInTheDocument();
    expect(screen.getAllByText("-").length).toBeGreaterThan(0);
  });

  it("flags a catalog source that failed to refresh", async () => {
    mock({
      sources: [
        {
          name: "official",
          url: "https://example.test/catalog",
          trusted: true,
          last_checked_at: "2026-09-14T10:00:00Z",
          last_error: "connection refused",
        },
      ],
    });
    customRender(<ProvidersPanel />);

    expect(await screen.findByText("official")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
  });
});
