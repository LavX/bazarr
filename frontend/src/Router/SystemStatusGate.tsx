import { FunctionComponent, ReactNode } from "react";
import { Alert, Button, Center, LoadingOverlay, Stack } from "@mantine/core";
import { useSystemStatus } from "@/apis/hooks/system";

interface SystemStatusGateProps {
  children: (status: System.Status) => ReactNode;
}

const SystemStatusGate: FunctionComponent<SystemStatusGateProps> = ({
  children,
}) => {
  const { data, isError, error, isFetching, refetch } = useSystemStatus();

  if (data !== undefined) {
    return children(data);
  }

  if (isError) {
    return (
      <Center mih="60vh" p="md">
        <Alert
          color="red"
          title="Bazarr+ did not answer"
          role="alert"
          maw={520}
        >
          <Stack gap="sm" align="flex-start">
            {error instanceof Error && error.message.length > 0
              ? error.message
              : "The system status request failed, so we cannot tell which translation page to open."}
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

  return <LoadingOverlay visible />;
};

export default SystemStatusGate;
