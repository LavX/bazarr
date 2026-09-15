import { useSystemSettings, useSystemStatus } from ".";

export function useEnabledStatus() {
  const { data } = useSystemSettings();

  return {
    sonarr: data?.general?.use_sonarr ?? false,
    radarr: data?.general?.use_radarr ?? false,
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
