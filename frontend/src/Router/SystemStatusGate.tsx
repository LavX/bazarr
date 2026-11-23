import { FunctionComponent, ReactNode } from "react";
import { Alert, Button, Center, LoadingOverlay, Stack } from "@mantine/core";
import { useTranslationRouteStatus } from "@/apis/hooks/translation";

interface SystemStatusGateProps {
  children: (runtime: "ready" | "legacy") => ReactNode;
  unavailableFallback?: ReactNode;
}

const SystemStatusGate: FunctionComponent<SystemStatusGateProps> = ({
  children,
  unavailableFallback,
}) => {
  const { runtime, isError, error, isFetching, refetch } =
    useTranslationRouteStatus();
  const unavailable = runtime === "unavailable";

  if (unavailable && unavailableFallback !== undefined) {
    return unavailableFallback;
  }

  if (isError || unavailable) {
    const title = unavailable
      ? "Translation runtime unavailable"
      : "Bazarr+ did not answer";
    let message =
      "The system status request failed, so we cannot tell which translation page to open.";
    if (unavailable) {
      message = "Translation is enabled. Try again to check if it is ready.";
    } else if (error instanceof Error && error.message.length > 0) {
      message = error.message;
    }

    return (
      <Center mih="60vh" p="md">
        <Alert color="red" title={title} role="alert" maw={520}>
          <Stack gap="sm" align="flex-start">
            {message}
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

  if (runtime !== undefined) {
    return children(runtime);
  }

  return <LoadingOverlay visible />;
};

export default SystemStatusGate;
