/* eslint-disable camelcase -- transport names. */
import { FunctionComponent, useState } from "react";
import {
  Alert,
  Button,
  Code,
  Group,
  List,
  Modal,
  Stack,
  Text,
} from "@mantine/core";
import { useQueryClient } from "@tanstack/react-query";
import { useProviderHubInstall } from "@/apis/hooks/providerHub";
import { QueryKeys } from "@/apis/queries/keys";
import type { TranslationAvailableEngine } from "@/types/translation";

export interface InstallConsentProps {
  /** The engine to install, or null while nothing is being asked. */
  engine: TranslationAvailableEngine | null;
  onClose: () => void;
  /** Called once the plugin is installed and the page has its new state. */
  onInstalled?: (engine: TranslationAvailableEngine) => void;
}

/** What installing an engine means, asked once, before the install job starts. */
const InstallConsent: FunctionComponent<InstallConsentProps> = ({
  engine,
  onClose,
  onInstalled,
}) => {
  const client = useQueryClient();
  const install = useProviderHubInstall({ inlineErrors: true });
  const [problem, setProblem] = useState("");
  const destinations = engine?.destinations ?? [];
  // A failure belongs to the attempt it came from: closing the question forgets it.
  const close = () => {
    setProblem("");
    onClose();
  };

  const run = async () => {
    if (!engine) {
      return;
    }
    setProblem("");
    try {
      await install.mutateAsync({
        source: engine.source,
        provider_id: engine.plugin_id,
        version: engine.version,
      });
      await client.invalidateQueries({ queryKey: [QueryKeys.Translation] });
      onInstalled?.(engine);
      close();
    } catch (error) {
      setProblem(
        error instanceof Error && error.message
          ? `The install did not finish: ${error.message}`
          : "The install did not finish. Check the Subtitle Hub activity for the reason.",
      );
    }
  };

  return (
    <Modal
      opened={engine !== null}
      onClose={install.isPending ? () => undefined : close}
      title={engine ? `Install ${engine.name}?` : ""}
      centered
    >
      {engine && (
        <Stack>
          <Text size="sm">
            Bazarr+ downloads {engine.name} {engine.version} from the plugin
            catalog and runs it as a plugin on this machine.
          </Text>
          {destinations.length > 0 && (
            <>
              <Text size="sm">
                When it translates, the subtitle text
                {engine.free ? "" : ", and your key,"} is sent to:
              </Text>
              <List size="sm" spacing={4}>
                {destinations.map((destination) => (
                  <List.Item key={destination}>
                    <Code>{destination}</Code>
                  </List.Item>
                ))}
              </List>
            </>
          )}
          <Text size="sm" c="dimmed">
            You can remove it at any time in the Subtitle Hub.
          </Text>
          {problem && (
            <Alert color="red" role="alert">
              {problem}
            </Alert>
          )}
          <Group justify="flex-end">
            <Button
              variant="default"
              onClick={close}
              disabled={install.isPending}
            >
              Cancel
            </Button>
            <Button onClick={() => void run()} loading={install.isPending}>
              Install
            </Button>
          </Group>
        </Stack>
      )}
    </Modal>
  );
};

export default InstallConsent;
