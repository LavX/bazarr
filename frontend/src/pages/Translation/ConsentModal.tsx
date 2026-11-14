import { FunctionComponent } from "react";
import { Button, Code, Group, List, Modal, Stack, Text } from "@mantine/core";

export interface ConsentModalProps {
  /** The new addresses the save would send data to. Empty closes the modal. */
  destinations: string[];
  busy?: boolean;
  onAccept: () => void;
  onCancel: () => void;
}

/** The question before a save that adds an address the user typed. */
const ConsentModal: FunctionComponent<ConsentModalProps> = ({
  destinations,
  busy,
  onAccept,
  onCancel,
}) => {
  const one = destinations.length === 1;
  return (
    <Modal
      opened={destinations.length > 0}
      onClose={onCancel}
      title="Send subtitles to a new address?"
      centered
    >
      <Stack>
        <Text size="sm">
          Saving this sends the subtitle text to translate, and the key if you
          entered one, to {one ? "this address" : "these addresses"}. Bazarr+
          cannot check who runs {one ? "it" : "them"}.
        </Text>
        <List size="sm" spacing={4}>
          {destinations.map((destination) => (
            <List.Item key={destination}>
              <Code>{destination}</Code>
            </List.Item>
          ))}
        </List>
        <Text size="sm" c="dimmed">
          A key you saved earlier is never sent to a changed address. You enter
          it again for the new one.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={onAccept} loading={busy}>
            {one ? "Send to this address" : "Send to these addresses"}
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
};

export default ConsentModal;
