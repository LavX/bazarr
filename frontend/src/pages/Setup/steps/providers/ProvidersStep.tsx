import { FC, useState } from "react";
import { useProviderHubProviders } from "@/apis/hooks";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import ProviderConfigureStage from "./ProviderConfigureStage";
import ProviderInstallStage from "./ProviderInstallStage";

type Stage = "install" | "configure";

/**
 * Providers onboarding step. It has two sub-stages: install (pick + install
 * providers, which restarts Bazarr+) and configure (enable + set credentials).
 *
 * The initial stage is derived from whether any providers are already
 * installed. This is what makes the post-restart resume work: after installing
 * triggers a restart and the wizard hard-redirects back to /setup, the
 * persisted step lands here, installed providers now exist, and we open
 * straight on the configure stage.
 */
const ProvidersStep: FC<WizardStepProps> = ({ onNext, onBack }) => {
  const { data: providers } = useProviderHubProviders();
  const hasInstalled = (providers ?? []).length > 0;

  const [stage, setStage] = useState<Stage>(() =>
    hasInstalled ? "configure" : "install",
  );

  if (stage === "configure") {
    return (
      <ProviderConfigureStage
        onNext={onNext}
        onBack={onBack}
        onInstallMore={() => setStage("install")}
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
      onUseInstalled={() => setStage("configure")}
      onBack={onBack}
    />
  );
};

export default ProvidersStep;
