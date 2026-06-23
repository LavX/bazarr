import ProvidersStep from "./providers/ProvidersStep";
import ArrStep from "./ArrStep";
import FinishStep from "./FinishStep";
import GeneralStep from "./GeneralStep";
import LanguagesStep from "./LanguagesStep";
import MediaServerStep from "./MediaServerStep";
import type { WizardStepDef } from "./types";
import WelcomeStep from "./WelcomeStep";

/**
 * Ordered registry of onboarding steps. Phase 2 ships the Welcome step; Phase 3
 * adds the Sonarr/Radarr connection steps and the optional media-server step;
 * Phase 4 adds the languages + profile step. Later phases push providers and
 * general steps here.
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
  {
    key: "media-servers",
    label: "Media Servers",
    optional: true,
    Component: MediaServerStep,
  },
  {
    key: "languages",
    label: "Languages",
    Component: LanguagesStep,
  },
  {
    key: "providers",
    label: "Providers",
    Component: ProvidersStep,
  },
  {
    key: "general",
    label: "General",
    optional: true,
    Component: GeneralStep,
  },
  {
    key: "finish",
    label: "Finish",
    Component: FinishStep,
  },
];

export type { WizardStepDef, WizardStepProps } from "./types";
