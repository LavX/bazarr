import { useArrInstances } from "@/apis/hooks/arrInstances";
import { useSystemSettings } from "@/apis/hooks/system";

/**
 * Derives whether a fresh install still needs the first-run onboarding wizard.
 *
 * needsOnboarding is true ONLY when the instance has not been touched at all:
 * setup is not marked complete, there are no arr instances, neither legacy
 * use_sonarr/use_radarr flag is set, and no subtitle provider is enabled. Any
 * single signal of prior configuration flips it to false so we never trap a
 * configured user on /setup.
 */
export function useOnboardingState(): {
  needsOnboarding: boolean;
  isLoading: boolean;
} {
  const { data: settings, isLoading: settingsLoading } = useSystemSettings();
  const { data: arrInstances, isLoading: instancesLoading } = useArrInstances();

  const isLoading = settingsLoading || instancesLoading;

  const general = settings?.general;
  const hasInstances = (arrInstances?.length ?? 0) > 0;
  const hasProviders = (general?.enabled_providers?.length ?? 0) > 0;

  const needsOnboarding =
    !general?.setup_complete &&
    !hasInstances &&
    !general?.use_sonarr &&
    !general?.use_radarr &&
    !hasProviders;

  return { needsOnboarding, isLoading };
}
