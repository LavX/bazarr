import { FunctionComponent, useId, useState } from "react";
import { Button, Group, Modal, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useQueryClient } from "@tanstack/react-query";
import { QueryKeys } from "@/apis/queries/keys";
import client from "@/apis/raw/client";

interface RegenerateDialogProps {
  opened: boolean;
  onClose: () => void;
}

const RegenerateDialog: FunctionComponent<RegenerateDialogProps> = ({
  opened,
  onClose,
}) => {
  const descriptionId = useId();
  const [loading, setLoading] = useState(false);
  const queryClient = useQueryClient();

  const handleRegenerate = async () => {
    setLoading(true);
    try {
      await client.axios.post("system/compat/regenerate");
      void queryClient.invalidateQueries({
        queryKey: [QueryKeys.System, QueryKeys.Settings],
      });
      notifications.show({
        title: "Secrets regenerated",
        message:
          "All compat secrets have been regenerated. Clients must re-paste the new token.",
        color: "green",
      });
      onClose();
    } catch {
      notifications.show({
        title: "Regenerate failed",
        message: "Could not regenerate secrets. Check the server logs.",
        color: "red",
      });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Modal
      opened={opened}
      onClose={onClose}
      title="Regenerate all compat secrets?"
      centered
      role="alertdialog"
      aria-describedby={descriptionId}
    >
      <Stack gap="md">
        <Text size="sm" id={descriptionId}>
          This will regenerate the API token, JWT secret, and file_id HMAC
          secret. All external clients must re-paste the new token and log in
          again. All outstanding file_id values will become invalid. This action
          is irreversible.
        </Text>
        <Group justify="flex-end" gap="sm">
          <Button variant="default" onClick={onClose} autoFocus>
            Cancel
          </Button>
          <Button color="red" loading={loading} onClick={handleRegenerate}>
            Regenerate
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
};

export default RegenerateDialog;
