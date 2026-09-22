import type { MediaServerDraftSummary } from "@/pages/Setup/useOnboardingSelection";
import MediaServerConfigureStep from "./mediaServer/MediaServerConfigureStep";
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

export const MEDIA_SERVER_SEGMENT = "Media servers";
export const MEDIA_SERVER_PICKER_KEY = "media-servers";

/** The step key for one media server draft. Stable for the draft's lifetime. */
export function mediaServerStepKey(draftId: string): string {
  return `media-server-${draftId}`;
}

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
    phase: "start",
    Component: WelcomeStep,
  },
  {
    key: "intent",
    label: "Your setup",
    phase: "start",
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
    phase: "connect",
    optional: true,
    paths: ["library"],
    Component: (p) => <ArrStep kind="sonarr" {...p} />,
  },
  {
    key: "radarr",
    label: "Radarr",
    phase: "connect",
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
    phase: "connect",
    optional: true,
    paths: ["library"],
    Component: (p) => <ArrStep kind="sportarr" {...p} />,
  },
  {
    // On both paths. A Jellyfin user with no Sonarr was never shown this step
    // at all, though connecting a media server has nothing to do with running
    // an arr: it is what makes Bazarr+ refresh the server after a download.
    key: MEDIA_SERVER_PICKER_KEY,
    label: "Media servers",
    phase: "connect",
    segment: MEDIA_SERVER_SEGMENT,
    optional: true,
    Component: MediaServerStep,
  },
  {
    // On both paths. Connecting Seerr is what makes the request button on a
    // title work, and the reader with no arr instance is exactly the one who
    // reaches for it, so the Discover path is the last place to hide it.
    key: "seerr",
    label: "Seerr",
    phase: "connect",
    optional: true,
    Component: SeerrStep,
  },
  {
    key: "languages",
    label: "Languages",
    phase: "subtitles",
    requiredReason:
      "Bazarr+ cannot fetch a subtitle without knowing what language you want it in.",
    Component: LanguagesStep,
  },
  {
    key: "providers",
    label: "Providers",
    phase: "subtitles",
    requiredReason:
      "Without at least one provider there is nothing for Bazarr+ to fetch subtitles from.",
    Component: ProvidersStep,
  },
  {
    key: "translator",
    label: "Translation",
    phase: "subtitles",
    optional: true,
    Component: TranslatorStep,
  },
  {
    key: "general",
    label: "General",
    phase: "finish",
    optional: true,
    Component: GeneralStep,
  },
  {
    key: "finish",
    label: "Finish",
    phase: "finish",
    Component: FinishStep,
  },
];

export interface WizardStepInput {
  intent: WizardIntent | null;
  mediaServers: MediaServerDraftSummary[];
}

/**
 * The steps to walk, for an intent and a media server selection.
 *
 * Pure and total: the same state in gives the same list out, and an empty
 * selection gives the picker on its own. `paths` is the static filter over the
 * fixed registry and stays exactly that; generation sits beside it rather than
 * being folded into it, because a filter cannot invent a step and this has to.
 *
 * Before the intent is answered the whole registry stands, so nothing pretends
 * to know which path runs. A draft that has been saved generates nothing: its
 * server exists, and the picker shows it as connected instead.
 */
export function buildSteps({
  intent,
  mediaServers,
}: WizardStepInput): WizardStepDef[] {
  const base =
    intent === null
      ? ONBOARDING_STEPS
      : ONBOARDING_STEPS.filter(
          (step) => step.paths === undefined || step.paths.includes(intent),
        );

  const generated: WizardStepDef[] = mediaServers
    .filter((draft) => draft.instanceId === undefined)
    .map((draft) => ({
      key: mediaServerStepKey(draft.draftId),
      // Named for the kind, not the draft's editable name: the label feeds the
      // step list, and a list that changes shape on every keystroke in the
      // Name field would remount the form under the cursor.
      label: draft.kind,
      phase: "connect",
      segment: MEDIA_SERVER_SEGMENT,
      optional: true,
      draftId: draft.draftId,
      Component: MediaServerConfigureStep,
    }));

  if (generated.length === 0) {
    return base;
  }

  const at = base.findIndex((step) => step.key === MEDIA_SERVER_PICKER_KEY);
  if (at < 0) {
    return base;
  }
  return [...base.slice(0, at + 1), ...generated, ...base.slice(at + 1)];
}

export type { WizardIntent, WizardStepDef, WizardStepProps } from "./types";
