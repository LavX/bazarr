import { FunctionComponent, useState } from "react";
import { useNavigate } from "react-router";
import { Alert, Button, Stack } from "@mantine/core";
import { useSettingsMutation } from "@/apis/hooks";
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
 */
const RunSetupAgain: FunctionComponent = () => {
  const navigate = useNavigate();
  const mutation = useSettingsMutation();
  const [error, setError] = useState<string | null>(null);

  const handleClick = () => {
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
      >
        Run first-time setup
      </Button>
      {error !== null && (
        <Alert color="red" title="Could not reopen setup">
          {error}
        </Alert>
      )}
    </Stack>
  );
};

export default RunSetupAgain;
