import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import SettingsSchedulerView from "@/pages/Settings/Scheduler";
import { customRender, screen } from "@/tests";
import server from "@/tests/mocks/node";

describe("Scheduler settings", () => {
  beforeEach(() => {
    server.use(
      http.get("/api/system/settings", () =>
        HttpResponse.json({
          general: {
            theme: "auto",
            use_sonarr: true,
            use_radarr: true,
            use_sportarr: true,
            wanted_search_frequency: 6,
            wanted_search_frequency_movie: 6,
            wanted_search_frequency_sports: 6,
            upgrade_subs: true,
            upgrade_frequency: 12,
            days_to_upgrade_subs: 7,
            upgrade_manual: true,
          },
          sonarr: {
            series_sync: 60,
            full_update: "Daily",
            full_update_day: 6,
            full_update_hour: 4,
            use_ffprobe_cache: true,
            sync_only_monitored_series: false,
            sync_only_monitored_episodes: false,
          },
          radarr: {
            movies_sync: 60,
            full_update: "Daily",
            full_update_day: 6,
            full_update_hour: 4,
            use_ffprobe_cache: true,
            sync_only_monitored_movies: false,
          },
          sportarr: {
            sports_sync: 60,
            full_update: "Daily",
            full_update_day: 6,
            full_update_hour: 4,
            use_ffprobe_cache: true,
            sync_only_monitored_leagues: false,
            sync_only_monitored_events: false,
          },
          backup: { frequency: "Weekly", day: 6, hour: 3, retention: 31 },
        }),
      ),
    );
  });

  it("carries the sports rows beside their series and movie siblings", async () => {
    customRender(<SettingsSchedulerView />);

    // The sync section now covers three kinds, so its header says so.
    expect(await screen.findByText("Library Sync")).toBeInTheDocument();
    expect(screen.getByText("Sync with Sportarr")).toBeInTheDocument();
    expect(screen.getByText("Sync Only Monitored Leagues")).toBeInTheDocument();
    expect(screen.getByText("Sync Only Monitored Events")).toBeInTheDocument();
    expect(
      screen.getByText("Update All Sports Subtitles from Disk"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Search for Missing Sports Subtitles"),
    ).toBeInTheDocument();
  });

  it("keeps the series and movie rows untouched", async () => {
    customRender(<SettingsSchedulerView />);

    expect(await screen.findByText("Sync with Sonarr")).toBeInTheDocument();
    expect(screen.getByText("Sync with Radarr")).toBeInTheDocument();
    expect(
      screen.getByText("Update All Episode Subtitles from Disk"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Update All Movie Subtitles from Disk"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Search for Missing Series Subtitles"),
    ).toBeInTheDocument();
  });
});
