import { FC, ReactNode } from "react";
import { Button, Group, Stack, Text } from "@mantine/core";

interface Props {
  onNext: () => void;
  onBack?: () => void;
  /** Persists before advancing. Omit when the primary button only advances. */
  onContinue?: () => void;
  continueLabel?: string;
  continuePending?: boolean;
  /**
   * A second action for this step, beside the primary one. Test lives here
   * rather than up among the fields: it is the last thing a reader does before
   * pressing Continue, and it was landing half a screen away from it.
   */
  secondary?: ReactNode;
  /** One line above the row, for why the secondary action cannot run yet. */
  note?: ReactNode;
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
  secondary,
  note,
}) => (
  <Stack gap={6}>
    {note && (
      <Text size="sm" c="dimmed" ta="right">
        {note}
      </Text>
    )}
    <Group justify="space-between">
      <Group gap="sm">
        {onBack && (
          <Button variant="default" onClick={onBack}>
            Back
          </Button>
        )}
      </Group>
      <Group gap="sm">
        {secondary}
        <Button onClick={onContinue ?? onNext} loading={continuePending}>
          {continueLabel}
        </Button>
      </Group>
    </Group>
  </Stack>
);

export default StepActions;
