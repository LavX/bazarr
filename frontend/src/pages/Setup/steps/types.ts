import type { FC } from "react";

export interface WizardStepProps {
  onNext: () => void; // advance + persist
  onBack?: () => void; // go back one step
}

export interface WizardStepDef {
  key: string; // stable id e.g. "welcome"
  label: string; // Stepper label
  Component: FC<WizardStepProps>;
  optional?: boolean; // renders a "Skip this step" affordance
}
