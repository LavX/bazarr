import { useEffect, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Group,
  Paper,
  Select,
  Stack,
  Text,
  Title,
} from "@mantine/core";
import { faRefresh } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  usePlexAuthValidationQuery,
  usePlexSelectedServerQuery,
  usePlexServerSelectionMutation,
  usePlexServersQuery,
} from "@/apis/hooks/plex";
import ConnectionsCard from "./ConnectionsCard";
import styles from "@/pages/Settings/Plex/ServerSection.module.scss";

const ServerSection = () => {
  // Internal state management
  const [selectedServer, setSelectedServer] = useState<Plex.Server | null>(
    null,
  );
  const [isSelecting, setIsSelecting] = useState(false);
  const [isSaved, setIsSaved] = useState(false);
  const [wasAuthenticated, setWasAuthenticated] = useState(false);

  // Use hooks to fetch data internally
  const { data: authData } = usePlexAuthValidationQuery();
  const {
    data: servers = [],
    error: serversError,
    refetch: refetchServers,
  } = usePlexServersQuery();
  const { mutateAsync: selectServerMutation } =
    usePlexServerSelectionMutation();
  const { data: savedSelectedServer } = usePlexSelectedServerQuery({
    enabled: Boolean(authData?.valid && authData?.auth_method === "oauth"),
  });

  // Determine authentication status
  const isAuthenticated = Boolean(
    authData?.valid && authData?.auth_method === "oauth",
  );

  // Reset state when authentication changes from false to true (re-authentication)
  useEffect(() => {
    if (isAuthenticated && !wasAuthenticated) {
      setSelectedServer(null);
      setIsSelecting(false);
      setIsSaved(false);
      setWasAuthenticated(true);
    } else if (!isAuthenticated && wasAuthenticated) {
      setWasAuthenticated(false);
    }
  }, [isAuthenticated, wasAuthenticated]);

  // Consolidated server selection and saving logic
  const selectAndSaveServer = async (server: Plex.Server) => {
    if (!server.bestConnection) return;

    setIsSelecting(true);
    try {
      await selectServerMutation({
        machineIdentifier: server.machineIdentifier,
        name: server.name,
        uri: server.bestConnection.uri,
        local: server.bestConnection.local,
        connections: server.connections?.map((conn) => conn.uri) || [
          server.bestConnection.uri,
        ],
      });
      setIsSaved(true);
      // The selection is persisted server-side by the plex/select-server
      // mutation above. Do NOT stage anything into the settings form here:
      // these were unprefixed keys ("plex_server"/"plex_server_name") with no
      // consumer, so the backend ignored them while they still inflated the
      // unsaved-changes count and fired the leave-page prompt (including on the
      // automatic single-server selection after OAuth).
    } catch {
      // Error is handled by the mutation hook
    } finally {
      setIsSelecting(false);
    }
  };

  // Handle server selection
  const handleServerSelect = async () => {
    if (!selectedServer) return;
    await selectAndSaveServer(selectedServer);
  };

  // Handle server change
  const handleSelectedServerChange = (server: Plex.Server | null) => {
    setSelectedServer(server);
    setIsSaved(false);
  };

  // Unified initialization logic
  const handleInitialization = () => {
    // First priority: initialize from saved server
    if (savedSelectedServer && !selectedServer && !isSaved) {
      setSelectedServer(savedSelectedServer);
      setIsSaved(true);
      return;
    }

    // Second priority: auto-select single server
    if (
      isAuthenticated &&
      servers.length === 1 &&
      servers[0].bestConnection &&
      !selectedServer &&
      !isSaved &&
      !savedSelectedServer
    ) {
      const server = servers[0];
      setSelectedServer(server);
      void selectAndSaveServer(server);
    }
  };

  // Run initialization when data is available
  useEffect(() => {
    if (isAuthenticated && (savedSelectedServer || servers.length > 0)) {
      handleInitialization();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    isAuthenticated,
    savedSelectedServer,
    servers.length,
    selectedServer,
    isSaved,
  ]);
  if (!isAuthenticated) {
    return null;
  }

  return (
    <Paper withBorder p="lg" className={styles.serverSection}>
      <Stack gap="lg">
        <Title order={4}>Plex Servers</Title>

        {serversError && (
          <Alert color="red" variant="light">
            Failed to load servers: {serversError.message}
          </Alert>
        )}

        {isAuthenticated && servers.length === 0 && !serversError ? (
          <Badge size="md">Testing server connections...</Badge>
        ) : servers.length === 0 ? (
          <Stack gap="sm">
            <Text>No servers found.</Text>
            <Button
              onClick={() => refetchServers()}
              variant="light"
              color="gray"
            >
              Refresh
            </Button>
          </Stack>
        ) : servers.length === 1 ? (
          // Single server - show simplified interface
          <Stack gap="md">
            <Group justify="space-between" align="center">
              <Stack gap="xs" className={styles.flexContainer}>
                <Group gap="xs">
                  <Text>
                    {servers[0].name} ({servers[0].platform} - v
                    {servers[0].version})
                  </Text>
                  {isSaved ? (
                    <Badge color="green" size="sm">
                      Connected
                    </Badge>
                  ) : !servers[0].bestConnection ? (
                    <Badge color="red" size="sm">
                      Unavailable
                    </Badge>
                  ) : null}
                </Group>
              </Stack>
              <ActionIcon
                variant="light"
                color="gray"
                size="lg"
                onClick={() => refetchServers()}
                title="Refresh server list"
              >
                <FontAwesomeIcon icon={faRefresh} size="sm" />
              </ActionIcon>
            </Group>
            {selectedServer && (
              <ConnectionsCard
                servers={servers}
                selectedServerId={selectedServer.machineIdentifier}
              />
            )}
          </Stack>
        ) : (
          // Multiple servers - show selection interface
          <Stack gap="md">
            <Group className={styles.serverSelectGroup}>
              <Select
                label="Select server"
                placeholder="Choose a server..."
                data={servers.map((server: Plex.Server) => ({
                  value: server.machineIdentifier,
                  label: `${server.name} (${server.platform} - v${server.version})${!server.bestConnection ? " (Unavailable)" : ""}`,
                  disabled: !server.bestConnection,
                }))}
                value={selectedServer?.machineIdentifier || null}
                onChange={(value: string | null) => {
                  const server = value
                    ? servers.find(
                        (s: Plex.Server) => s.machineIdentifier === value,
                      ) || null
                    : null;
                  handleSelectedServerChange(server);
                }}
                className={styles.serverSelectField}
                searchable
              />
              <Button
                variant="filled"
                color="brand"
                disabled={!selectedServer || isSelecting}
                loading={isSelecting}
                onClick={handleServerSelect}
              >
                Select Server
              </Button>
              <ActionIcon
                variant="light"
                color="gray"
                size="lg"
                onClick={() => refetchServers()}
                className={styles.refreshButton}
                title="Refresh server list"
              >
                <FontAwesomeIcon icon={faRefresh} size="sm" />
              </ActionIcon>
            </Group>

            {isSaved && selectedServer && (
              <Alert color="brand" variant="light">
                Server saved: "{selectedServer.name}" (v
                {servers.find(
                  (s: Plex.Server) =>
                    s.machineIdentifier === selectedServer.machineIdentifier,
                )?.version ||
                  selectedServer.version ||
                  "Unknown"}
                )
              </Alert>
            )}

            {selectedServer && (
              <ConnectionsCard
                servers={servers}
                selectedServerId={selectedServer.machineIdentifier}
              />
            )}
          </Stack>
        )}
      </Stack>
    </Paper>
  );
};

export default ServerSection;
