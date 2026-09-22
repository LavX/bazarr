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

/**
 * The four stretches of the wizard, in order.
 *
 * The rail counts phases, not steps. A step total cannot be shown honestly:
 * before the intent is answered nobody knows which path runs, and the media
 * server segment grows and shrinks as servers are ticked, so a denominator
 * would change under the reader mid-setup. The phases never do.
 */
export type WizardPhase = "start" | "connect" | "subtitles" | "finish";

export const WIZARD_PHASES: WizardPhase[] = [
  "start",
  "connect",
  "subtitles",
  "finish",
];

export const PHASE_LABELS: Record<WizardPhase, string> = {
  start: "Getting started",
  connect: "Connect",
  subtitles: "Subtitles",
  finish: "Finish",
};

export interface WizardStepProps {
  onNext: () => void; // advance + persist
  onBack?: () => void; // go back one step
  // Which media server draft this step configures. Only the generated media
  // server steps carry one; every other step ignores it.
  draftId?: string;
}

export interface WizardStepDef {
  key: string; // stable id e.g. "welcome"
  // The media server draft a generated step configures, handed to its
  // component by the shell. It is data rather than a closure on purpose: a
  // component built per draft is a new function type every time the step list
  // is rebuilt, and React remounts on a changed type, so one draft finishing
  // its save threw away the test result, the loaded libraries and the pending
  // state of the form the reader was filling in beside it.
  draftId?: string;
  label: string; // Stepper label
  phase: WizardPhase;
  Component: FC<WizardStepProps>;
  // Steps that belong to one stretch of the same subject, counted together in
  // the header so a generated segment reads "Media servers 2 of 3" instead of
  // moving a total nobody can predict.
  segment?: string;
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
