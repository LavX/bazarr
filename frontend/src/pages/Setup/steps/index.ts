import WelcomeStep from "./WelcomeStep";
import type { WizardStepDef } from "./types";

/**
 * Ordered registry of onboarding steps. Phase 2 ships only the Welcome step;
 * later phases push connections, languages, providers, and general steps here.
 */
export const ONBOARDING_STEPS: WizardStepDef[] = [
  {
    key: "welcome",
    label: "Welcome",
    Component: WelcomeStep,
  },
];

export type { WizardStepDef, WizardStepProps } from "./types";
