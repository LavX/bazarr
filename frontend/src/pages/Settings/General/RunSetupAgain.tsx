import { FunctionComponent, useState } from "react";
import { useNavigate } from "react-router";
import { Alert, Button, Stack, Text } from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
import { useHasPendingChanges, useOnlyRefreshPending } from "@/contexts";
import { clearPersistedOnboarding } from "@/pages/Setup/setupCompleteCache";

/**
 * The way back into the first-run wizard.
 *
 * Leaving setup marks it complete, and nothing in the application linked to
 * /setup afterwards: the only way back was typing a URL nobody has a reason to
 * know exists. The wizard's own "Set up later" promises this control by name,
 * so it has to be here for that promise to be worth anything.
 *
 * Clearing the flag first is what makes the visit a real first run again: the
 * flag is the one signal that says setup has been dealt with, so a wizard
 * opened on top of it would be undone by the next thing that reads it.
 *
 * That write comes before the navigation, and the settings page answers a
 * navigation with its unsaved-changes prompt while it holds edits. Keep
 * editing then left the reader on Settings with the flag already cleared, and
 * the next visit to the home page opened a wizard nobody had asked for. So
 * the control waits until nothing is pending: the decision about the edits is
 * the reader's, taken before anything is written.
 */
const RunSetupAgain: FunctionComponent = () => {
  const navigate = useNavigate();
  const mutation = useSettingsMutation();
  const [error, setError] = useState<string | null>(null);
  const hasPendingChanges = useHasPendingChanges();
  const onlyRefreshPending = useOnlyRefreshPending();

  const handleClick = () => {
    // aria-disabled keeps the button in the tab order, so it does not stop the
    // click by itself.
    if (hasPendingChanges || mutation.isPending) {
      return;
    }
    setError(null);
    mutation.mutate(
      { "settings-general-setup_complete": false },
      {
        onSuccess: () => {
          // A step, intent or draft this browser kept from an earlier run
          // would reopen the wizard there instead of at Welcome.
          clearPersistedOnboarding();
          navigate("/setup");
        },
        onError: () =>
          setError(
            "Bazarr+ could not reopen setup. Nothing has changed. Check that Bazarr+ is reachable and try again.",
          ),
      },
    );
  };

  return (
    <Stack gap="sm" align="flex-start">
      <Button
        variant="default"
        onClick={handleClick}
        loading={mutation.isPending}
        // Not `disabled`: a disabled button leaves the tab order, so a keyboard
        // or screen reader user never reached it and never heard why. This one
        // stays focusable, says it is unavailable, and reads out the reason.
        aria-disabled={hasPendingChanges || undefined}
        data-disabled={hasPendingChanges || undefined}
        aria-describedby={hasPendingChanges ? "run-setup-pending" : undefined}
      >
        Run first-time setup
      </Button>
      {hasPendingChanges && (
        <Text id="run-setup-pending" size="sm" c="dimmed">
          {onlyRefreshPending
            ? "Retry the application refresh first, from the Retry refresh button. Setup opens on its own page, and leaving this one would ask what to do with the saved settings."
            : "Save or discard your unsaved changes first. Setup opens on its own page, and leaving this one would ask what to do with them."}
        </Text>
      )}
      {error !== null && (
        <Alert color="red" title="Could not reopen setup">
          {error}
        </Alert>
      )}
    </Stack>
  );
};

export default RunSetupAgain;
