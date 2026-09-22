import { FC } from "react";
import StepLayout from "@/pages/Setup/StepLayout";
import type { WizardStepProps } from "@/pages/Setup/steps/types";
import { useOnboardingSelection } from "@/pages/Setup/useOnboardingSelection";
import InstanceServerForm from "./InstanceServerForm";
import PlexServerForm from "./PlexServerForm";
import StepActions from "./StepActions";

type Props = WizardStepProps;

/**
 * One media server, one screen.
 *
 * The picker used to render the chosen kind's form under its own cards, which
 * only ever worked because exactly one kind could be chosen. A server per step
 * is what lets the reader connect several, and it gives each form a component
 * of its own, which is what the test and libraries hooks need: they close over
 * the connection values and reset when one changes.
 */
const MediaServerConfigureStep: FC<Props> = ({ draftId, onNext, onBack }) => {
  const { drafts } = useOnboardingSelection();
  const draft = drafts.find((entry) => entry.draftId === draftId);

  if (!draft) {
    // The cursor moves off a step whose draft is gone, so this is a frame at
    // most. Say something rather than render an empty card.
    return (
      <StepLayout
        title="Media server"
        titleOrder={3}
        description="This server is no longer selected."
        actions={<StepActions onNext={onNext} onBack={onBack} />}
      />
    );
  }

  return draft.kind === "plex" ? (
    <PlexServerForm draft={draft} onNext={onNext} onBack={onBack} />
  ) : (
    <InstanceServerForm draft={draft} onNext={onNext} onBack={onBack} />
  );
};

export default MediaServerConfigureStep;
