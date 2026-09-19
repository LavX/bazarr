import ProvidersStep from "./providers/ProvidersStep";
import ArrStep from "./ArrStep";
import FinishStep from "./FinishStep";
import GeneralStep from "./GeneralStep";
import IntentStep from "./IntentStep";
import LanguagesStep from "./LanguagesStep";
import MediaServerStep from "./MediaServerStep";
import SeerrStep from "./SeerrStep";
import TranslatorStep from "./TranslatorStep";
import type { WizardIntent, WizardStepDef } from "./types";
import WelcomeStep from "./WelcomeStep";

/**
 * Ordered registry of onboarding steps.
 *
 * `optional` is the whole skip contract: the shell renders one skip control
 * from it, and a step that is not optional carries `requiredReason`, the one
 * line explaining why there is nothing to skip. Steps never render a skip of
 * their own; index.test.tsx fails if one does.
 *
 * `paths` filters the rail by the answer to the intent step. Nothing here is
 * required, because Bazarr+ runs with no arr instance at all: Discover is the
 * homepage and works against an empty library.
 */
export const ONBOARDING_STEPS: WizardStepDef[] = [
  {
    key: "welcome",
    label: "Welcome",
    Component: WelcomeStep,
  },
  {
    key: "intent",
    label: "Your setup",
    // The one question the rest of the wizard follows from. Skipping it would
    // leave the rail with nothing to filter on, so it has no skip; the answer
    // itself commits to nothing and is changeable in Settings.
    requiredReason:
      "This only picks which steps we show you. Nothing here is locked in.",
    Component: IntentStep,
  },
  {
    key: "sonarr",
    label: "Sonarr",
    optional: true,
    paths: ["library"],
    Component: (p) => <ArrStep kind="sonarr" {...p} />,
  },
  {
    key: "radarr",
    label: "Radarr",
    optional: true,
    paths: ["library"],
    Component: (p) => <ArrStep kind="radarr" {...p} />,
  },
  {
    // Optional like Radarr. A Sportarr user had to finish the wizard and then
    // find Settings > Connections, because the wizard offered no way to
    // connect one at all.
    key: "sportarr",
    label: "Sportarr",
    optional: true,
    paths: ["library"],
    Component: (p) => <ArrStep kind="sportarr" {...p} />,
  },
  {
    key: "media-servers",
    label: "Media Servers",
    optional: true,
    paths: ["library"],
    Component: MediaServerStep,
  },
  {
    // On both paths. Connecting Seerr is what makes the request button on a
    // title work, and the reader with no arr instance is exactly the one who
    // reaches for it, so the Discover path is the last place to hide it.
    key: "seerr",
    label: "Seerr",
    optional: true,
    Component: SeerrStep,
  },
  {
    key: "languages",
    label: "Languages",
    requiredReason:
      "Bazarr+ cannot fetch a subtitle without knowing what language you want it in.",
    Component: LanguagesStep,
  },
  {
    key: "providers",
    label: "Providers",
    requiredReason:
      "Without at least one provider there is nothing for Bazarr+ to fetch subtitles from.",
    Component: ProvidersStep,
  },
  {
    key: "translator",
    label: "Translation",
    optional: true,
    Component: TranslatorStep,
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

/**
 * The steps to walk for a given intent. Before the question is answered the
 * whole registry stands, so the rail on Welcome shows everything that could be
 * asked rather than pretending to know.
 */
export function stepsForIntent(intent: WizardIntent | null): WizardStepDef[] {
  if (intent === null) {
    return ONBOARDING_STEPS;
  }
  return ONBOARDING_STEPS.filter(
    (step) => step.paths === undefined || step.paths.includes(intent),
  );
}

export type { WizardIntent, WizardStepDef, WizardStepProps } from "./types";
