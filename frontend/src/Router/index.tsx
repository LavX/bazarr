import {
  createContext,
  FunctionComponent,
  lazy,
  useContext,
  useMemo,
} from "react";
import { createBrowserRouter, RouterProvider } from "react-router";
import {
  faClock,
  faCogs,
  faExclamationTriangle,
  faFileExcel,
  faFilm,
  faLaptop,
  faPlay,
} from "@fortawesome/free-solid-svg-icons";
import { useBadges } from "@/apis/hooks";
import { useEnabledStatus } from "@/apis/hooks/site";
import App from "@/App";
import { Lazy } from "@/components/async";
import Authentication from "@/pages/Authentication";
import NotFound from "@/pages/errors/NotFound";
import { Environment } from "@/utilities";
import Redirector from "./Redirector";
import { RouterNames } from "./RouterNames";
import { CustomRouteObject } from "./type";

// Lazy-load all page components for smaller initial bundle
const SeriesView = lazy(() => import("@/pages/Series"));
const Episodes = lazy(() => import("@/pages/Episodes"));
const MovieView = lazy(() => import("@/pages/Movies"));
const MovieDetailView = lazy(() => import("@/pages/Movies/Details"));
const SeriesHistoryView = lazy(() => import("@/pages/History/Series"));
const MoviesHistoryView = lazy(() => import("@/pages/History/Movies"));
const HistoryStats = lazy(
  () => import("@/pages/History/Statistics/HistoryStats"),
);
const WantedSeriesView = lazy(() => import("@/pages/Wanted/Series"));
const WantedMoviesView = lazy(() => import("@/pages/Wanted/Movies"));
const BlacklistSeriesView = lazy(() => import("@/pages/Blacklist/Series"));
const BlacklistMoviesView = lazy(() => import("@/pages/Blacklist/Movies"));
const SettingsGeneralView = lazy(() => import("@/pages/Settings/General"));
const SettingsLanguagesView = lazy(() => import("@/pages/Settings/Languages"));
const SettingsProvidersView = lazy(() => import("@/pages/Settings/Providers"));
const SettingsSubtitlesView = lazy(() => import("@/pages/Settings/Subtitles"));
const SettingsSonarrView = lazy(() => import("@/pages/Settings/Sonarr"));
const SettingsRadarrView = lazy(() => import("@/pages/Settings/Radarr"));
const SettingsPlexView = lazy(() => import("@/pages/Settings/Plex"));
const SettingsTranslatorView = lazy(
  () => import("@/pages/Settings/Translator"),
);
const SettingsNotificationsView = lazy(
  () => import("@/pages/Settings/Notifications"),
);
const SettingsSchedulerView = lazy(() => import("@/pages/Settings/Scheduler"));
const SettingsUIView = lazy(() => import("@/pages/Settings/UI"));
const SystemTasksView = lazy(() => import("@/pages/System/Tasks"));
const SystemLogsView = lazy(() => import("@/pages/System/Logs"));
const SystemProvidersView = lazy(() => import("@/pages/System/Providers"));
const SystemBackupsView = lazy(() => import("@/pages/System/Backups"));
const SystemStatusView = lazy(() => import("@/pages/System/Status"));
const SystemReleasesView = lazy(() => import("@/pages/System/Releases"));
const SystemAnnouncementsView = lazy(
  () => import("@/pages/System/Announcements"),
);
const SubtitleEditor = lazy(() => import("@/pages/SubtitleEditor"));
const SubtitleEditorPage = lazy(
  () => import("@/pages/SubtitleEditor/EditorPage"),
);

function useRoutes(): CustomRouteObject[] {
  const { data } = useBadges();
  const { sonarr, radarr } = useEnabledStatus();

  return useMemo(
    () => [
      {
        path: "/",
        element: <App></App>,
        children: [
          {
            index: true,
            element: <Redirector></Redirector>,
          },
          {
            icon: faPlay,
            name: "Series",
            path: "series",
            badge: data?.sonarr_signalr,
            hidden: !sonarr,
            children: [
              {
                index: true,
                element: (
                  <Lazy>
                    <SeriesView />
                  </Lazy>
                ),
              },
              {
                path: ":id",
                element: (
                  <Lazy>
                    <Episodes />
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faFilm,
            name: "Movies",
            path: "movies",
            badge: data?.radarr_signalr,
            hidden: !radarr,
            children: [
              {
                index: true,
                element: (
                  <Lazy>
                    <MovieView />
                  </Lazy>
                ),
              },
              {
                path: ":id",
                element: (
                  <Lazy>
                    <MovieDetailView />
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faClock,
            name: "History",
            path: "history",
            hidden: !sonarr && !radarr,
            children: [
              {
                path: "series",
                name: "Episodes",
                hidden: !sonarr,
                element: (
                  <Lazy>
                    <SeriesHistoryView />
                  </Lazy>
                ),
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !radarr,
                element: (
                  <Lazy>
                    <MoviesHistoryView />
                  </Lazy>
                ),
              },
              {
                path: "stats",
                name: "Statistics",
                element: (
                  <Lazy>
                    <HistoryStats></HistoryStats>
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faExclamationTriangle,
            name: "Missing Subtitles",
            path: "wanted",
            hidden: !sonarr && !radarr,
            children: [
              {
                name: "Episodes",
                path: "series",
                badge: data?.episodes,
                hidden: !sonarr,
                element: (
                  <Lazy>
                    <WantedSeriesView />
                  </Lazy>
                ),
              },
              {
                name: "Movies",
                path: "movies",
                badge: data?.movies,
                hidden: !radarr,
                element: (
                  <Lazy>
                    <WantedMoviesView />
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faFileExcel,
            name: "Excluded",
            path: "blacklist",
            hidden: !sonarr && !radarr,
            children: [
              {
                path: "series",
                name: "Episodes",
                hidden: !sonarr,
                element: (
                  <Lazy>
                    <BlacklistSeriesView />
                  </Lazy>
                ),
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !radarr,
                element: (
                  <Lazy>
                    <BlacklistMoviesView />
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faCogs,
            name: "Settings",
            path: "settings",
            children: [
              {
                path: "general",
                name: "General",
                element: (
                  <Lazy>
                    <SettingsGeneralView />
                  </Lazy>
                ),
              },
              {
                path: "languages",
                name: "Languages",
                element: (
                  <Lazy>
                    <SettingsLanguagesView />
                  </Lazy>
                ),
              },
              {
                path: "providers",
                name: "Subtitle Sources",
                element: (
                  <Lazy>
                    <SettingsProvidersView />
                  </Lazy>
                ),
              },
              {
                path: "subtitles",
                name: "Subtitles",
                element: (
                  <Lazy>
                    <SettingsSubtitlesView />
                  </Lazy>
                ),
              },
              {
                path: "sonarr",
                name: "Sonarr",
                element: (
                  <Lazy>
                    <SettingsSonarrView />
                  </Lazy>
                ),
              },
              {
                path: "radarr",
                name: "Radarr",
                element: (
                  <Lazy>
                    <SettingsRadarrView />
                  </Lazy>
                ),
              },
              {
                path: "plex",
                name: "Plex",
                element: (
                  <Lazy>
                    <SettingsPlexView />
                  </Lazy>
                ),
              },
              {
                path: "translator",
                name: "AI Translator",
                element: (
                  <Lazy>
                    <SettingsTranslatorView />
                  </Lazy>
                ),
              },
              {
                path: "notifications",
                name: "Notifications",
                element: (
                  <Lazy>
                    <SettingsNotificationsView />
                  </Lazy>
                ),
              },
              {
                path: "scheduler",
                name: "Scheduler",
                element: (
                  <Lazy>
                    <SettingsSchedulerView />
                  </Lazy>
                ),
              },
              {
                path: "ui",
                name: "UI",
                element: (
                  <Lazy>
                    <SettingsUIView />
                  </Lazy>
                ),
              },
            ],
          },
          {
            icon: faLaptop,
            name: "System",
            path: "system",
            children: [
              {
                path: "tasks",
                name: "Tasks",
                element: (
                  <Lazy>
                    <SystemTasksView />
                  </Lazy>
                ),
              },
              {
                path: "logs",
                name: "Logs",
                element: (
                  <Lazy>
                    <SystemLogsView />
                  </Lazy>
                ),
              },
              {
                path: "providers",
                name: "Provider Status",
                badge: data?.providers,
                element: (
                  <Lazy>
                    <SystemProvidersView />
                  </Lazy>
                ),
              },
              {
                path: "backup",
                name: "Backups",
                element: (
                  <Lazy>
                    <SystemBackupsView />
                  </Lazy>
                ),
              },
              {
                path: "status",
                name: "Status",
                badge: data?.status,
                element: (
                  <Lazy>
                    <SystemStatusView />
                  </Lazy>
                ),
              },
              {
                path: "releases",
                name: "Releases",
                element: (
                  <Lazy>
                    <SystemReleasesView />
                  </Lazy>
                ),
              },
              {
                path: "announcements",
                name: "Announcements",
                badge: data?.announcements,
                element: (
                  <Lazy>
                    <SystemAnnouncementsView />
                  </Lazy>
                ),
              },
            ],
          },
          {
            path: "subtitles/preview/:mediaType/:mediaId/:language",
            hidden: true,
            element: (
              <Lazy>
                <SubtitleEditor></SubtitleEditor>
              </Lazy>
            ),
          },
          {
            path: "subtitles/edit/:mediaType/:mediaId/:language",
            hidden: true,
            element: (
              <Lazy>
                <SubtitleEditorPage></SubtitleEditorPage>
              </Lazy>
            ),
          },
          {
            path: "*",
            hidden: true,
            element: <NotFound></NotFound>,
          },
        ],
      },
      {
        path: RouterNames.Auth,
        hidden: true,
        element: <Authentication></Authentication>,
      },
    ],
    [
      data?.episodes,
      data?.movies,
      data?.providers,
      data?.sonarr_signalr,
      data?.radarr_signalr,
      data?.announcements,
      data?.status,
      radarr,
      sonarr,
    ],
  );
}

const RouterItemContext = createContext<CustomRouteObject[]>([]);

export const Router: FunctionComponent = () => {
  const routes = useRoutes();

  // TODO: Move this outside the function component scope
  const router = useMemo(
    () =>
      createBrowserRouter(routes, {
        basename: Environment.baseUrl,
      }),
    [routes],
  );

  return (
    <RouterItemContext.Provider value={routes}>
      <RouterProvider router={router}></RouterProvider>
    </RouterItemContext.Provider>
  );
};

export function useRouteItems() {
  return useContext(RouterItemContext);
}
