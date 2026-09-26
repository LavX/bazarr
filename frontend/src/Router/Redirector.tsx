import { FunctionComponent, useEffect } from "react";
import { useNavigate } from "react-router";
import { Alert, Button, Center, LoadingOverlay, Stack } from "@mantine/core";
import { useSystemSettings } from "@/apis/hooks";
import { useOnboardingState } from "@/pages/Setup/useOnboardingState";

const Redirector: FunctionComponent = () => {
  const { data, isError, error, isFetching, refetch } = useSystemSettings();
  const { needsOnboarding, isLoading } = useOnboardingState();

  const navigate = useNavigate();

  useEffect(() => {
    // Both reads have to have answered before this decides. needsOnboarding is
    // true by default while they are in flight, so choosing early sends a
    // configured install to the wizard, and choosing on settings alone sends a
    // fresh one past it. Neither is recoverable: this replaces the entry, so
    // the reader never sees a history entry for what they were sent past.
    if (isLoading || data === undefined) return;

    // A fresh install meets the wizard before any normal routing.
    if (needsOnboarding) {
      navigate("/setup", { replace: true });
      return;
    }

    // Everything else, configured or not, opens on Discover.
    navigate("/discover", { replace: true });
  }, [data, navigate, needsOnboarding, isLoading]);

  // A settings read that failed is not a settings read still in flight. Without
  // this branch the landing page of a Bazarr+ that cannot answer is a spinner
  // that never stops, with no message and nothing to press, which is the first
  // thing a broken install shows its owner.
  if (isError && data === undefined) {
    return (
      <Center mih="60vh" p="md">
        <Alert color="red" title="Bazarr+ did not answer" maw={520}>
          <Stack gap="sm" align="flex-start">
            {error instanceof Error && error.message.length > 0
              ? error.message
              : "The settings request failed, so we cannot tell which page to open."}
            <Button
              variant="default"
              loading={isFetching}
              onClick={() => void refetch()}
            >
              Try again
            </Button>
          </Stack>
        </Alert>
      </Center>
    );
  }

  return <LoadingOverlay visible></LoadingOverlay>;
};

export default Redirector;
