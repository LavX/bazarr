import { FunctionComponent, useRef, useState } from "react";
import { ActionIcon, Group, TextInput, Tooltip } from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { notifications } from "@mantine/notifications";
import {
  faCopy,
  faEye,
  faEyeSlash,
  faRotate,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import RegenerateDialog from "./RegenerateDialog";

const TokenField: FunctionComponent = () => {
  const token = useSettingValue<string>("settings-compat_endpoint-token");
  const [revealed, setRevealed] = useState(false);
  const [dialogOpen, { open: openDialog, close: closeDialog }] =
    useDisclosure(false);
  const regenerateButtonRef = useRef<HTMLButtonElement>(null);

  const displayValue = token ?? "";

  const handleRevealToggle = () => {
    const next = !revealed;
    setRevealed(next);
    notifications.show({
      message: next ? "Token revealed" : "Token hidden",
      autoClose: 2000,
    });
  };

  const handleCopy = async () => {
    if (!displayValue) {
      notifications.show({
        title: "Nothing to copy",
        message: "No token value available.",
        color: "yellow",
      });
      return;
    }

    if (!window.isSecureContext) {
      notifications.show({
        title: "Cannot copy",
        message:
          "Clipboard access requires a secure context (HTTPS or http://localhost).",
        color: "yellow",
      });
      return;
    }

    try {
      await navigator.clipboard.writeText(displayValue);
      notifications.show({
        title: "Copied",
        message: "Token copied to clipboard.",
        color: "green",
      });
    } catch {
      notifications.show({
        title: "Copy failed",
        message: "Could not copy to clipboard.",
        color: "red",
      });
    }
  };

  return (
    <>
      <TextInput
        label="API Token"
        description="Share this token with subtitle clients that connect to the compat endpoint."
        value={displayValue}
        readOnly
        type={revealed ? "text" : "password"}
        rightSectionWidth={112}
        rightSection={
          <Group gap={4} wrap="nowrap">
            <Tooltip label={revealed ? "Hide token" : "Reveal token"}>
              <ActionIcon
                ref={null}
                variant="subtle"
                size="lg"
                aria-label={revealed ? "Hide token" : "Reveal token"}
                aria-pressed={revealed}
                onClick={handleRevealToggle}
              >
                <FontAwesomeIcon icon={revealed ? faEyeSlash : faEye} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Copy token">
              <ActionIcon
                variant="subtle"
                size="lg"
                aria-label="Copy token to clipboard"
                onClick={handleCopy}
              >
                <FontAwesomeIcon icon={faCopy} />
              </ActionIcon>
            </Tooltip>
            <Tooltip label="Regenerate secrets">
              <ActionIcon
                ref={regenerateButtonRef}
                variant="subtle"
                size="lg"
                color="red"
                aria-label="Regenerate compat secrets"
                onClick={openDialog}
              >
                <FontAwesomeIcon icon={faRotate} />
              </ActionIcon>
            </Tooltip>
          </Group>
        }
      />
      <RegenerateDialog
        opened={dialogOpen}
        onClose={() => {
          closeDialog();
          regenerateButtonRef.current?.focus();
        }}
      />
    </>
  );
};

export default TokenField;
