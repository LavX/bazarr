import { FunctionComponent } from "react";
import {
  Alert,
  Button,
  Code,
  CopyButton,
  Group,
  Modal,
  Stack,
} from "@mantine/core";
import { faCopy } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";

interface TokenRevealModalProps {
  token: string | null;
  onClose: () => void;
  // Freshly created/rotated keys are one-time secrets and warn they cannot be
  // retrieved again. The shared legacy token is re-viewable, so it skips that
  // warning and uses copy that reflects an existing, persistent secret.
  oneTime?: boolean;
}

const TokenRevealModal: FunctionComponent<TokenRevealModalProps> = ({
  token,
  onClose,
  oneTime = true,
}) => (
  <Modal
    opened={token != null}
    onClose={onClose}
    title={oneTime ? "Copy your API key" : "Legacy Default key token"}
  >
    <Stack gap="sm">
      <Alert color="yellow">
        {oneTime
          ? "This key is shown only once. Copy it now and store it securely. You can rotate it later, but it cannot be retrieved again."
          : "This is the current shared token for the legacy Default key. Keep it secret. Anyone holding it can use the endpoint until you rotate it."}
      </Alert>
      <Group gap="xs" wrap="nowrap">
        <Code style={{ flex: 1, overflowWrap: "anywhere" }}>{token}</Code>
        <CopyButton value={token ?? ""}>
          {({ copied, copy }) => (
            <Button
              size="xs"
              color={copied ? "teal" : "blue"}
              leftSection={<FontAwesomeIcon icon={faCopy} />}
              onClick={copy}
            >
              {copied ? "Copied" : "Copy"}
            </Button>
          )}
        </CopyButton>
      </Group>
      <Group justify="flex-end">
        <Button variant="default" onClick={onClose}>
          Done
        </Button>
      </Group>
    </Stack>
  </Modal>
);

export default TokenRevealModal;
