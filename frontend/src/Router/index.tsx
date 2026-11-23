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
  faLanguage,
  faLaptop,
  faPlay,
  faPlug,
  faStore,
  faTowerBroadcast,
  faTrophy,
} from "@fortawesome/free-solid-svg-icons";
import { useBadges } from "@/apis/hooks";
import { useMediaNavigation } from "@/apis/hooks/site";
import { useTranslationRuntime } from "@/apis/hooks/translation";
import App from "@/App";
import { Lazy } from "@/components/async";
import Authentication from "@/pages/Authentication";
import BlacklistMoviesView from "@/pages/Blacklist/Movies";
import BlacklistSeriesView from "@/pages/Blacklist/Series";
import BlacklistSportsView from "@/pages/Blacklist/Sports";
import DiscoverView from "@/pages/Discover";
import DistributionHubView from "@/pages/DistributionHub";
import Episodes from "@/pages/Episodes";
import NotFound from "@/pages/errors/NotFound";
import MoviesHistoryView from "@/pages/History/Movies";
import SeriesHistoryView from "@/pages/History/Series";
import SportsHistoryView from "@/pages/History/Sports";
import MovieView from "@/pages/Movies";
import MovieDetailView from "@/pages/Movies/Details";
import PluginHubView from "@/pages/PluginHub";
import SeriesView from "@/pages/Series";
import SettingsConnectionsView from "@/pages/Settings/Connections";
import SettingsGeneralView from "@/pages/Settings/General";
import SettingsLanguagesView from "@/pages/Settings/Languages";
import SettingsNotificationsView from "@/pages/Settings/Notifications";
import SettingsProvidersView from "@/pages/Settings/Providers";
import SettingsSchedulerView from "@/pages/Settings/Scheduler";
import SettingsSubtitlesView from "@/pages/Settings/Subtitles";
import SettingsTranslatorView from "@/pages/Settings/Translator";
import SettingsUIView from "@/pages/Settings/UI";
import OnboardingWizardView from "@/pages/Setup/OnboardingWizard";
import {
  SourceMovieDetailView,
  SourceSeriesDetailView,
} from "@/pages/SourceDetail";
import Sports from "@/pages/Sports";
import SportsEvents from "@/pages/SportsEvents";
import SystemAnnouncementsView from "@/pages/System/Announcements";
import SystemBackupsView from "@/pages/System/Backups";
import SystemLogsView from "@/pages/System/Logs";
import SystemProvidersView from "@/pages/System/Providers";
import SystemReleasesView from "@/pages/System/Releases";
import SystemTasksView from "@/pages/System/Tasks";
import TranslationView from "@/pages/Translation";
import WantedMoviesView from "@/pages/Wanted/Movies";
import WantedSeriesView from "@/pages/Wanted/Series";
import WantedSportsView from "@/pages/Wanted/Sports";
import { Environment } from "@/utilities";
import ArrMediaRoute from "./ArrMediaRoute";
import Redirector from "./Redirector";
import { RouterNames } from "./RouterNames";
import TranslationGate from "./TranslationGate";
import { CustomRouteObject } from "./type";

const StatisticsView = lazy(() => import("@/pages/Statistics"));
const SystemStatusView = lazy(() => import("@/pages/System/Status"));
const SubtitleEditor = lazy(() => import("@/pages/SubtitleEditor"));
const SubtitleEditorPage = lazy(
  () => import("@/pages/SubtitleEditor/EditorPage"),
);

export function useRoutes(): CustomRouteObject[] {
  const { data } = useBadges();
  const {
    series,
    movies,
    sports,
    folderSeries,
    folderMovies,
    seriesActivity,
    movieActivity,
    sportsActivity,
    sonarr,
    radarr,
    sonarrBadges,
    radarrBadges,
    sportarrBadges,
    folderSeriesSearchable,
    folderMoviesSearchable,
  } = useMediaNavigation();
  // The Translation page exists only while the v2 translation runtime is on.
  const translationRuntime = useTranslationRuntime();

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
            badge: sonarrBadges ? data?.sonarr_signalr : undefined,
            hidden: !series,
            children: [
              {
                index: true,
                element: (
                  <ArrMediaRoute kind="sonarr" sourceBrowsable={folderSeries}>
                    <SeriesView />
                  </ArrMediaRoute>
                ),
              },
              {
                path: "source/:id",
                element: <SourceSeriesDetailView />,
              },
              {
                path: ":id",
                element: (
                  <ArrMediaRoute kind="sonarr">
                    <Episodes />
                  </ArrMediaRoute>
                ),
              },
            ],
          },
          {
            icon: faFilm,
            name: "My movies",
            path: "movies",
            badge: radarrBadges ? data?.radarr_signalr : undefined,
            hidden: !movies,
            children: [
              {
                index: true,
                element: (
                  <ArrMediaRoute kind="radarr" sourceBrowsable={folderMovies}>
                    <MovieView />
                  </ArrMediaRoute>
                ),
              },
              {
                path: "source/:id",
                element: <SourceMovieDetailView />,
              },
              {
                path: ":id",
                element: (
                  <ArrMediaRoute kind="radarr">
                    <MovieDetailView />
                  </ArrMediaRoute>
                ),
              },
            ],
          },
          {
            icon: faTrophy,
            name: "Sports",
            path: "sports",
            // The event stream's live state, the counterpart of the SignalR
            // badges on Series and Movies. The endpoint has reported it all
            // along and the nav never showed it, so there was no way to see at
            // a glance whether Bazarr was still connected to Sportarr.
            badge: sportarrBadges ? data?.sportarr_sse : undefined,
            hidden: !sports,
            children: [
              { index: true, element: <Sports /> },
              { path: ":id", element: <SportsEvents /> },
            ],
          },
          {
            icon: faClock,
            name: "History",
            path: "history",
            hidden: !sonarr && !radarr && !sportsActivity,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportsActivity,
                element: <SportsHistoryView></SportsHistoryView>,
              },
              {
                path: "series",
                name: "Episodes",
                hidden: !sonarr,
                element: (
                  <ArrMediaRoute kind="sonarr">
                    <SeriesHistoryView />
                  </ArrMediaRoute>
                ),
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !radarr,
                element: (
                  <ArrMediaRoute kind="radarr">
                    <MoviesHistoryView />
                  </ArrMediaRoute>
                ),
              },
              {
                // Statistics lived here through 2.6, so old bookmarks point at it.
                path: "stats",
                hidden: true,
                element: <Navigate to="/system/statistics" replace />,
              },
            ],
          },
          {
            icon: faExclamationTriangle,
            name: "Missing",
            path: "wanted",
            hidden: !seriesActivity && !movieActivity && !sportsActivity,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportsActivity,
                badge: sportarrBadges ? data?.sports : undefined,
                element: <WantedSportsView></WantedSportsView>,
              },
              {
                name: "Episodes",
                path: "series",
                badge: sonarrBadges
                  ? data?.episodes
                  : folderSeriesSearchable
                    ? data?.source_episodes
                    : undefined,
                hidden: !seriesActivity,
                element: folderSeries ? (
                  <WantedSeriesView />
                ) : (
                  <ArrMediaRoute kind="sonarr">
                    <WantedSeriesView />
                  </ArrMediaRoute>
                ),
              },
              {
                name: "Movies",
                path: "movies",
                badge: radarrBadges
                  ? data?.movies
                  : folderMoviesSearchable
                    ? data?.source_movies
                    : undefined,
                hidden: !movieActivity,
                element: folderMovies ? (
                  <WantedMoviesView />
                ) : (
                  <ArrMediaRoute kind="radarr">
                    <WantedMoviesView />
                  </ArrMediaRoute>
                ),
              },
            ],
          },
          {
            icon: faFileExcel,
            name: "Excluded",
            path: "blacklist",
            hidden: !seriesActivity && !movieActivity && !sportsActivity,
            children: [
              {
                path: "sports",
                name: "Sports",
                hidden: !sportsActivity,
                element: <BlacklistSportsView></BlacklistSportsView>,
              },
              {
                path: "series",
                name: "Episodes",
                hidden: !seriesActivity,
                element: seriesActivity ? (
                  <BlacklistSeriesView />
                ) : (
                  <ArrMediaRoute kind="sonarr">
                    <BlacklistSeriesView />
                  </ArrMediaRoute>
                ),
              },
              {
                path: "movies",
                name: "Movies",
                hidden: !movieActivity,
                element: movieActivity ? (
                  <BlacklistMoviesView />
                ) : (
                  <ArrMediaRoute kind="radarr">
                    <BlacklistMoviesView />
                  </ArrMediaRoute>
                ),
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
            icon: faPlug,
            name: "Plugin Hub",
            path: "plugin-hub",
            element: <PluginHubView></PluginHubView>,
          },
          {
            icon: faLanguage,
            name: "Translation",
            path: "translation",
            // Out of the navigation while the runtime is off; the gate makes
            // its address Not Found. See TranslationGate for why the route
            // itself is always here.
            hidden: !translationRuntime,
            element: (
              <TranslationGate>
                <TranslationView />
              </TranslationGate>
            ),
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
                hidden: true,
                element: (
                  <Navigate
                    to="/subtitle-hub?tab=my-providers#metadata"
                    replace
                  />
                ),
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
                // The page lived under Settings while it was being built.
                path: "translation",
                hidden: true,
                element: <Navigate to="/translation" replace />,
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
                path: "statistics",
                name: "Statistics",
                element: (
                  <Lazy>
                    <StatisticsView></StatisticsView>
                  </Lazy>
                ),
              },
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
      {
        // The step key is part of the URL, so the wizard keeps history of its
        // own: browser Back walks one step back instead of leaving the
        // application, a reload resumes on the step it left, and a step can be
        // linked to. Bare /setup above still works and redirects to the step
        // the persisted cursor names.
        path: "/setup/:stepKey",
        hidden: true,
        element: <OnboardingWizardView></OnboardingWizardView>,
      },
    ],
    [
      data?.episodes,
      data?.movies,
      data?.source_episodes,
      data?.source_movies,
      data?.sports,
      data?.providers,
      data?.sonarr_signalr,
      data?.radarr_signalr,
      data?.sportarr_sse,
      data?.announcements,
      data?.status,
      series,
      movies,
      sports,
      folderSeries,
      folderMovies,
      seriesActivity,
      movieActivity,
      sportsActivity,
      sonarr,
      radarr,
      sonarrBadges,
      radarrBadges,
      sportarrBadges,
      folderSeriesSearchable,
      folderMoviesSearchable,
      translationRuntime,
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
