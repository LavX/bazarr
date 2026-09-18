import { FC } from "react";
import { Button, Group } from "@mantine/core";

interface Props {
  onNext: () => void;
  onBack?: () => void;
  /** Persists before advancing. Omit when the primary button only advances. */
  onContinue?: () => void;
  continueLabel?: string;
  continuePending?: boolean;
}

/**
 * Back and Continue, the row every state of the media-server step ends with.
 * Skipping is the wizard shell's control, rendered from the step's `optional`,
 * so no step and no sub-form here renders one of its own.
 */
const StepActions: FC<Props> = ({
  onNext,
  onBack,
  onContinue,
  continueLabel = "Continue",
  continuePending,
}) => (
  <Group justify="space-between">
    <Group gap="sm">
      {onBack && (
        <Button variant="default" onClick={onBack}>
          Back
        </Button>
      )}
    </Group>
    <Button onClick={onContinue ?? onNext} loading={continuePending}>
      {continueLabel}
    </Button>
  </Group>
);

export default StepActions;
