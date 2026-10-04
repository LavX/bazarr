import { FunctionComponent } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Card,
  Code,
  Group,
  Stack,
  Text,
  Tooltip,
} from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { faCopy } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useQueryClient } from "@tanstack/react-query";
import { isAxiosError } from "axios";
import {
  usePlexAuthValidationQuery,
  usePlexAutopulseConfigQuery,
} from "@/apis/hooks/plex";
import { QueryKeys } from "@/apis/queries/keys";
import styles from "@/pages/Settings/Plex/AutopulseSelector.module.scss";

// Bazarr holds no Plex token. The server says so with a 409 and this code, not
// a 401, which the client reads as the Bazarr session ending.
function isPlexSignInRequired(error: unknown): boolean {
  return (
    isAxiosError(error) &&
    error.response?.status === 409 &&
    error.response.data?.error_code === "sign_in_required"
  );
}

export type AutopulseSelectorProps = {
  label: string;
  description?: React.ReactNode;
};

const AutopulseSelector: FunctionComponent<AutopulseSelectorProps> = (
  props,
) => {
  const { label, description } = props;
  const queryClient = useQueryClient();

  // Check if user is authenticated with OAuth
  const { data: authData } = usePlexAuthValidationQuery();
  const isAuthenticated = Boolean(
    authData?.valid && authData?.auth_method === "oauth",
  );

  const {
    data: configData,
    refetch: refetchConfig,
    isFetching: isFetchingConfig,
  } = usePlexAutopulseConfigQuery({
    enabled: false,
    retry: false,
  });

  const handleGenerateAutopulseConfig = async () => {
    const result = await refetchConfig();

    if (result.isSuccess && result.data) {
      notifications.show({
        id: "autopulse-config",
        title: "Success",
        message: "Autopulse configuration generated successfully",
        color: "green",
      });
    } else if (result.isError) {
      const status = (result.error as { response?: { status?: number } })
        ?.response?.status;

      // Bazarr's own session has ended, and the app is on its way to the login
      // page, which says all there is to say.
      if (status === 401) {
        return;
      }

      const signInRequired = isPlexSignInRequired(result.error);
      if (signInRequired) {
        // The account above was read while Bazarr still held the token, so
        // read it again. Signed out, this panel then asks for a Plex sign-in.
        void queryClient.invalidateQueries({
          queryKey: [QueryKeys.Plex, "auth", "validate"],
        });
      }

      const errorMessage = signInRequired
        ? "Sign in to Plex to generate an Autopulse configuration."
        : status === 400
          ? "Unable to generate configuration. Please ensure the external webhook is configured and saved in Settings."
          : "Failed to generate Autopulse configuration. Please ensure Autopulse is running and supports the template API.";

      notifications.show({
        id: "autopulse-config",
        title: "Error",
        message: errorMessage,
        color: "red",
      });
    }
  };

  if (!isAuthenticated) {
    return (
      <Stack gap="xs" className={styles.autopulseSelector}>
        <Text fw={500} size="sm" className={styles.labelText}>
          {label}
        </Text>
        <Alert color="brand" variant="light" className={styles.alertMessage}>
          Enable Plex OAuth above to generate an Autopulse configuration.
        </Alert>
      </Stack>
    );
  }

  return (
    <Stack gap="xs" className={styles.autopulseSelector}>
      <div>
        <Text fw={500} size="sm" mb={2} className={styles.labelText}>
          {label}
        </Text>
        <Text size="xs" c="var(--bz-text-tertiary)">
          {description}
        </Text>
      </div>

      <Group gap="xs">
        <Button
          onClick={handleGenerateAutopulseConfig}
          loading={isFetchingConfig}
          size="sm"
          variant="light"
          className={styles.generateButton}
        >
          Generate Configuration
        </Button>

        {configData && (
          <Badge color="green" variant="light" size="sm">
            Dynamic
          </Badge>
        )}
      </Group>

      {configData && (
        <Card withBorder p="md" mt="md" className={styles.configCard}>
          <Group justify="space-between" align="center" mb="xs">
            <Group gap="xs">
              <Text size="sm" fw={600}>
                Autopulse Configuration
              </Text>
            </Group>
            <Tooltip label="Copy configuration">
              <ActionIcon
                variant="subtle"
                size="sm"
                onClick={async () => {
                  const yamlContent = configData?.config_yaml;

                  if (!yamlContent) {
                    notifications.show({
                      title: "Error",
                      message: "No configuration to copy",
                      color: "red",
                    });
                    return;
                  }

                  if (!window.isSecureContext) {
                    notifications.show({
                      title: "Cannot Copy",
                      message:
                        "Clipboard access requires a secure context (HTTPS or http://localhost). Please copy manually from the code block below.",
                      color: "yellow",
                    });
                    return;
                  }

                  try {
                    await navigator.clipboard.writeText(yamlContent);
                    notifications.show({
                      title: "Copied!",
                      message: "Autopulse configuration copied to clipboard",
                      color: "green",
                    });
                  } catch {
                    notifications.show({
                      title: "Copy Failed",
                      message:
                        "Failed to copy to clipboard. Please copy manually from the code block below.",
                      color: "red",
                    });
                  }
                }}
              >
                <FontAwesomeIcon icon={faCopy} />
              </ActionIcon>
            </Tooltip>
          </Group>

          <Code block className={styles.configCodeBlock}>
            {configData.config_yaml}
          </Code>

          <Stack gap="xs" mt="sm">
            <Text size="xs" c="var(--bz-text-tertiary)">
              <Text component="span" fw={600}>
                Server:
              </Text>{" "}
              {configData.server_name}
            </Text>

            {configData.rewrite_suggestion && (
              <Alert
                color={configData.rewrite_detected ? "yellow" : "brand"}
                variant="light"
                className={styles.alertMessage}
              >
                <Text size="xs">
                  <Text component="span" fw={600}>
                    Configuration Notes:
                  </Text>{" "}
                  {configData.rewrite_suggestion}
                </Text>
              </Alert>
            )}

            {configData.template_info && (
              <Text size="xs" c="var(--bz-text-tertiary)">
                {configData.template_info}
              </Text>
            )}
          </Stack>
        </Card>
      )}
    </Stack>
  );
};

export default AutopulseSelector;
