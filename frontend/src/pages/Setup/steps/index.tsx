import ArrStep from "./ArrStep";
import WelcomeStep from "./WelcomeStep";
import type { WizardStepDef } from "./types";

/**
 * Ordered registry of onboarding steps. Phase 2 ships the Welcome step; Phase 3
 * adds the Sonarr/Radarr connection steps. Later phases push languages,
 * providers, and general steps here.
 */
export const ONBOARDING_STEPS: WizardStepDef[] = [
  {
    key: "welcome",
    label: "Welcome",
    Component: WelcomeStep,
  },
  {
    key: "sonarr",
    label: "Sonarr",
    Component: (p) => <ArrStep kind="sonarr" required {...p} />,
  },
  {
    key: "radarr",
    label: "Radarr",
    optional: true,
    Component: (p) => <ArrStep kind="radarr" {...p} />,
  },
];

export type { WizardStepDef, WizardStepProps } from "./types";
