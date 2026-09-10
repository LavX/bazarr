import {
  createContext,
  FunctionComponent,
  lazy,
  useContext,
  useMemo,
} from "react";
import { createBrowserRouter, Navigate, RouterProvider } from "react-router";
import {
  faClock,
  faCogs,
  faCompass,
  faExclamationTriangle,
  faFileExcel,
  faFilm,
  faLaptop,
  faPlay,
  faStore,
  faTowerBroadcast,
  faTrophy,
} from "@fortawesome/free-solid-svg-icons";
import { useBadges } from "@/apis/hooks";
import { useEnabledStatus } from "@/apis/hooks/site";
import App from "@/App";
import { Lazy } from "@/components/async";
import Authentication from "@/pages/Authentication";
import BlacklistMoviesView from "@/pages/Blacklist/Movies";
import BlacklistSeriesView from "@/pages/Blacklist/Series";
import DiscoverView from "@/pages/Discover";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import DistributionHubView from "@/pages/DistributionHub";
import Episodes from "@/pages/Episodes";
import NotFound from "@/pages/errors/NotFound";
import MoviesHistoryView from "@/pages/History/Movies";
import SeriesHistoryView from "@/pages/History/Series";
import SportsHistoryView from "@/pages/History/Sports";
import MovieView from "@/pages/Movies";
import MovieDetailView from "@/pages/Movies/Details";
import SeriesView from "@/pages/Series";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import SettingsDiscoverView from "@/pages/Settings/Discover";
import SettingsGeneralView from "@/pages/Settings/General";
import SettingsLanguagesView from "@/pages/Settings/Languages";
import SettingsNotificationsView from "@/pages/Settings/Notifications";
import SettingsProvidersView from "@/pages/Settings/Providers";
import SettingsSchedulerView from "@/pages/Settings/Scheduler";
import SettingsSubtitlesView from "@/pages/Settings/Subtitles";
import SettingsTranslatorView from "@/pages/Settings/Translator";
import SettingsUIView from "@/pages/Settings/UI";
import OnboardingWizardView from "@/pages/Setup/OnboardingWizard";
import Sports from "@/pages/Sports";
import SportsEvents from "@/pages/SportsEvents";
import SystemAnnouncementsView from "@/pages/System/Announcements";
import SystemBackupsView from "@/pages/System/Backups";
import SystemLogsView from "@/pages/System/Logs";
import SystemProvidersView from "@/pages/System/Providers";
import SystemReleasesView from "@/pages/System/Releases";
import SystemTasksView from "@/pages/System/Tasks";
import WantedMoviesView from "@/pages/Wanted/Movies";
import WantedSeriesView from "@/pages/Wanted/Series";
import WantedSportsView from "@/pages/Wanted/Sports";
import { Environment } from "@/utilities";
import Redirector from "./Redirector";
import { RouterNames } from "./RouterNames";
import { CustomRouteObject } from "./type";

const HistoryStats = lazy(
  () => import("@/pages/History/Statistics/HistoryStats"),
);
const SystemStatusView = lazy(() => import("@/pages/System/Status"));
const SubtitleEditor = lazy(() => import("@/pages/SubtitleEditor"));
const SubtitleEditorPage = lazy(
  () => import("@/pages/SubtitleEditor/EditorPage"),
);

export function useRoutes(): CustomRouteObject[] {
  const { data } = useBadges();
  const { sonarr, radarr, sportarr } = useEnabledStatus();

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
            icon: faCompass,
            name: "Discover",
            path: "discover",
            element: <DiscoverView />,
          },
          {
            icon: faPlay,
            name: "My series",
            path: "series",
            badge: data?.sonarr_signalr,
            hidden: !sonarr,
            children: [
              {
                index: true,
                element: <SeriesView></SeriesView>,
              },
              {
                path: ":id",
                element: <Episodes></Episodes>,
              },
            ],
          },
          {
            icon: faFilm,
            name: "My movies",
            path: "movies",
            badge: data?.radarr_signalr,
            hidden: !radarr,
            children: [
              {
                index: true,
                element: <MovieView></MovieView>,
              },
              {
                path: ":id",
                element: <MovieDetailView></MovieDetailView>,
              },
            ],
          },
          {
            icon: faTrophy,
            name: "Sports",
            path: "sports",
            hidden: !sportarr,
            children: [
              { index: true, element: <Sports /> },
              { path: ":id", element: <SportsEvents /> },
            ],
          },
          {
            icon: faClock,
            name: "History",
            path: "history",
            hidden: !sonarr && !radarr && !sportarr,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportarr,
                element: <SportsHistoryView></SportsHistoryView>,
              },
              {
                path: "series",
                name: "Episodes",
                hidden: !sonarr,
                element: <SeriesHistoryView></SeriesHistoryView>,
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !radarr,
                element: <MoviesHistoryView></MoviesHistoryView>,
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
            name: "Missing",
            path: "wanted",
            hidden: !sonarr && !radarr && !sportarr,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportarr,
                badge: data?.sports,
                element: <WantedSportsView></WantedSportsView>,
              },
              {
                name: "Episodes",
                path: "series",
                badge: data?.episodes,
                hidden: !sonarr,
                element: <WantedSeriesView></WantedSeriesView>,
              },
              {
                name: "Movies",
                path: "movies",
                badge: data?.movies,
                hidden: !radarr,
                element: <WantedMoviesView></WantedMoviesView>,
              },
            ],
          },
          {
            icon: faFileExcel,
            name: "Excluded",
            path: "blacklist",
            hidden: !sonarr && !radarr && !sportarr,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportarr,
                element: <BlacklistSportsView></BlacklistSportsView>,
              },
              {
                path: "series",
                name: "Episodes",
                hidden: !sonarr,
                element: <BlacklistSeriesView></BlacklistSeriesView>,
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !radarr,
                element: <BlacklistMoviesView></BlacklistMoviesView>,
              },
            ],
          },
          {
            icon: faStore,
            name: "Subtitle Hub",
            path: "subtitle-hub",
            element: <SettingsProvidersView></SettingsProvidersView>,
          },
          {
            icon: faTowerBroadcast,
            name: "Distribution Hub",
            path: "distribution-hub",
            element: <DistributionHubView></DistributionHubView>,
          },
          {
            icon: faCogs,
            name: "Settings",
            path: "settings",
            children: [
              {
                path: "discover",
                name: "Discover",
                element: <SettingsDiscoverView></SettingsDiscoverView>,
              },
              {
                path: "connections",
                name: "Connections",
                element: <SettingsConnectionsView></SettingsConnectionsView>,
              },
              {
                path: "sonarr",
                hidden: true,
                element: <Navigate to="/settings/connections#sonarr" replace />,
              },
              {
                path: "radarr",
                hidden: true,
                element: <Navigate to="/settings/connections#radarr" replace />,
              },
              {
                path: "plex",
                hidden: true,
                element: <Navigate to="/settings/connections#plex" replace />,
              },
              {
                path: "jellyfin",
                hidden: true,
                element: (
                  <Navigate to="/settings/connections#jellyfin" replace />
                ),
              },
              {
                path: "instances",
                hidden: true,
                element: <Navigate to="/settings/connections" replace />,
              },
              {
                divider: "Subtitles",
                path: "languages",
                name: "Languages",
                element: <SettingsLanguagesView></SettingsLanguagesView>,
              },
              {
                path: "subtitles",
                name: "Subtitles",
                element: <SettingsSubtitlesView></SettingsSubtitlesView>,
              },
              {
                path: "translator",
                name: "AI Translator",
                element: <SettingsTranslatorView></SettingsTranslatorView>,
              },
              {
                path: "external",
                hidden: true,
                element: <Navigate to="/distribution-hub" replace />,
              },
              {
                divider: "Application",
                path: "general",
                name: "General",
                element: <SettingsGeneralView></SettingsGeneralView>,
              },
              {
                path: "notifications",
                name: "Notifications",
                element: (
                  <SettingsNotificationsView></SettingsNotificationsView>
                ),
              },
              {
                path: "scheduler",
                name: "Scheduler",
                element: <SettingsSchedulerView></SettingsSchedulerView>,
              },
              {
                path: "ui",
                name: "UI",
                element: <SettingsUIView></SettingsUIView>,
              },
              {
                path: "providers",
                hidden: true,
                element: <Navigate to="/subtitle-hub" replace />,
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
                element: <SystemTasksView></SystemTasksView>,
              },
              {
                path: "logs",
                name: "Logs",
                element: <SystemLogsView></SystemLogsView>,
              },
              {
                path: "providers",
                name: "Provider Status",
                badge: data?.providers,
                element: <SystemProvidersView></SystemProvidersView>,
              },
              {
                path: "backup",
                name: "Backups",
                element: <SystemBackupsView></SystemBackupsView>,
              },
              {
                path: "status",
                name: "Status",
                badge: data?.status,
                element: (
                  <Lazy>
                    <SystemStatusView></SystemStatusView>
                  </Lazy>
                ),
              },
              {
                path: "releases",
                name: "Releases",
                element: <SystemReleasesView></SystemReleasesView>,
              },
              {
                path: "announcements",
                name: "Announcements",
                badge: data?.announcements,
                element: <SystemAnnouncementsView></SystemAnnouncementsView>,
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
      {
        path: "/setup",
        hidden: true,
        element: <OnboardingWizardView></OnboardingWizardView>,
      },
    ],
    [
      data?.episodes,
      data?.movies,
      data?.sports,
      data?.providers,
      data?.sonarr_signalr,
      data?.radarr_signalr,
      data?.announcements,
      data?.status,
      radarr,
      sonarr,
      sportarr,
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
