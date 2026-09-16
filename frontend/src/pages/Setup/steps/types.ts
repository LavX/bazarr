import type { FC } from "react";

/**
 * What the user said they want Bazarr+ to do, asked once on the second screen.
 *
 * "library" is the classic install: Sonarr, Radarr or Sportarr feed the
 * library and Bazarr+ fetches subtitles for it. "discover" is the install that
 * needs no instance at all: search any title, preview, download to this device.
 * It is wizard-local and filters the step rail; nothing on the backend reads it
 * and nothing about it is locked in.
 */
export type WizardIntent = "library" | "discover";

export interface WizardStepProps {
  onNext: () => void; // advance + persist
  onBack?: () => void; // go back one step
}

export interface WizardStepDef {
  key: string; // stable id e.g. "welcome"
  label: string; // Stepper label
  Component: FC<WizardStepProps>;
  // The single source of skippability. The wizard shell renders the skip
  // control from this, with one label for every step; steps never render
  // their own (steps/index.test.tsx fails if one does).
  optional?: boolean;
  // Why a step that is not optional has no skip, shown in one line under the
  // card. Required on every non-optional step so "you cannot skip this" is
  // never left unexplained.
  requiredReason?: string;
  // Which intent paths show the step. Omitted means both.
  paths?: WizardIntent[];
}
