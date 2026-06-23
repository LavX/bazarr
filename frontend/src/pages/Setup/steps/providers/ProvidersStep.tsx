import { FC, useState } from "react";
import { Center, Loader } from "@mantine/core";
import { useProviderHubProviders } from "@/apis/hooks";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import ProviderConfigureStage from "./ProviderConfigureStage";
import ProviderInstallStage from "./ProviderInstallStage";

type Stage = "install" | "configure";

/**
 * Providers onboarding step. It has two sub-stages: install (pick + install
 * providers, which restarts Bazarr+) and configure (enable + set credentials).
 *
 * The stage is DERIVED from whether any providers are already installed, not
 * latched on first render. This is what makes the post-restart resume work:
 * after installing triggers a restart and the wizard hard-redirects back to
 * /setup, the page reloads and the installed-providers query starts empty while
 * it fetches. Latching the stage on that first (still-loading) render would
 * wrongly drop the user back on the install list. Instead we wait for the query
 * (loader), then derive: installed providers present -> configure. An explicit
 * user choice (`override`) wins so "install more" / "use installed" still work.
 */
const ProvidersStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const providersQuery = useProviderHubProviders();
  const hasInstalled = (providersQuery.data ?? []).length > 0;

  const [override, setOverride] = useState<Stage | null>(null);
  const stage: Stage = override ?? (hasInstalled ? "configure" : "install");

  // Wait for the installed-providers list before choosing a stage, so a resume
  // after the install-restart does not flash the install catalog first.
  if (override === null && providersQuery.isLoading) {
    return (
      <Center mih={240}>
        <Loader />
      </Center>
    );
  }

  if (stage === "configure") {
    return (
      <ProviderConfigureStage
        onNext={onNext}
        onBack={onBack}
        onInstallMore={() => setOverride("install")}
      />
    );
  }

  return (
    <ProviderInstallStage
      hasInstalled={hasInstalled}
      onInstalledNeedsRestart={() => {
        // The install stage owns the restart overlay + resume; nothing to do
        // here beyond letting it take over the view.
      }}
      onUseInstalled={() => setOverride("configure")}
      onBack={onBack}
    />
  );
};

export default ProvidersStep;
