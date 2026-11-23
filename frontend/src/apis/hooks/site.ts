import type { ArrKind } from "@/apis/raw/arrInstances";
import {
  getArrInstanceAvailability,
  isArrMediaRouteEnabled,
  useArrInstances,
} from "./arrInstances";
import { useSourceAvailability } from "./mediaSources";
import { useSystemSettings, useSystemStatus } from "./system";

export function useEnabledStatus() {
  const { data } = useSystemSettings();

  return {
    sportarr: data?.general?.use_sportarr ?? false,
    sonarr: data?.general?.use_sonarr ?? false,
    radarr: data?.general?.use_radarr ?? false,
  };
}

export function useMediaNavigation() {
  const { data: settings } = useSystemSettings();
  const general = settings?.general;
  const visible = settings?.media_sources?.visible === true;
  const { data: instances } = useArrInstances(
    visible &&
      Boolean(
        general?.use_sonarr || general?.use_radarr || general?.use_sportarr,
      ),
  );
  const sonarr = isArrMediaRouteEnabled("sonarr", settings, instances);
  const radarr = isArrMediaRouteEnabled("radarr", settings, instances);
  const sportarr = isArrMediaRouteEnabled("sportarr", settings, instances);
  const { availability } = useSourceAvailability();
  const series = sonarr || availability?.series.browsable === true;
  const movies = radarr || availability?.movie.browsable === true;
  const sports = sportarr || availability?.sports.browsable === true;
  // Hidden folders keep master-based navigation. The badge can still use
  // cached instance records from Connections or an Arr page.
  const showBadges = (kind: ArrKind, routeEnabled: boolean) =>
    routeEnabled &&
    (visible || getArrInstanceAvailability(kind, instances) !== false);
  const sonarrBadges = showBadges("sonarr", sonarr);
  const radarrBadges = showBadges("radarr", radarr);
  return {
    series,
    movies,
    sports,
    folderSeries: availability?.series.browsable === true,
    folderMovies: availability?.movie.browsable === true,
    seriesActivity: series || availability?.series.retained === true,
    movieActivity: movies || availability?.movie.retained === true,
    sportsActivity: sports || availability?.sports.retained === true,
    sonarr,
    radarr,
    sonarrBadges,
    radarrBadges,
    sportarrBadges: showBadges("sportarr", sportarr),
    wantedEpisodesBadge:
      sonarrBadges || availability?.series.searchable === true,
    wantedMoviesBadge: radarrBadges || availability?.movie.searchable === true,
  };
}

export function useShowOnlyDesired() {
  const { data } = useSystemSettings();
  return data?.general?.embedded_subs_show_desired ?? false;
}

export function useInstanceName() {
  const { data } = useSystemSettings();
  return data?.general?.instance_name;
}

/**
 * Suffix for every document title: the instance name followed by the running
 * version, e.g. "Bazarr+ v2.7.0". Falls back to the name alone when the version
 * is not known yet, and to the default name while settings are still loading.
 */
export function useAppTitle() {
  // A config predating the empty-name coercion can still hold a blank string,
  // which "??" would let through as an empty title base.
  const name = useInstanceName()?.trim() || "Bazarr+";
  const { data: status } = useSystemStatus();
  const version = status?.bazarr_version;

  if (!version || version === "unknown") {
    return name;
  }

  // The API reports the version without its leading "v".
  return `${name} ${version.startsWith("v") ? version : `v${version}`}`;
}
