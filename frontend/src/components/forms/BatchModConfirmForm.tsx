import { FunctionComponent } from "react";
import { Button, Divider, Group, Stack, Text } from "@mantine/core";
import { notifications } from "@mantine/notifications";
import { useBatchAction } from "@/apis/hooks";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { useModals, withModal } from "@/modules/modals";

/* eslint-disable camelcase -- keys match backend action identifiers */
const ACTION_LABELS: Record<string, string> = {
  OCR_fixes: "OCR Fixes",
  common: "Common Fixes",
  remove_HI: "Remove Hearing Impaired",
  remove_tags: "Remove Style Tags",
  fix_uppercase: "Fix Uppercase",
  reverse_rtl: "Reverse RTL",
  "scan-disk": "Scan Disk",
  "search-missing": "Search Missing Subtitles",
  upgrade: "Upgrade Subtitles",
};
/* eslint-enable camelcase */

interface BatchModConfirmFormProps {
  items: BatchItem[];
  action: BatchAction;
}

const BatchModConfirmForm: FunctionComponent<BatchModConfirmFormProps> = ({
  items,
  action,
}) => {
  const { mutateAsync, isPending } = useBatchAction();
  const modals = useModals();
  const label = ACTION_LABELS[action] ?? action;

  const handleSubmit = async () => {
    try {
      const result = await mutateAsync({ items, action });

      notifications.show({
        title: `${label} Queued`,
        message: `Queued: ${result.queued}, Skipped: ${result.skipped}${result.errors.length > 0 ? `, Errors: ${result.errors.length}` : ""}`,
        color: result.errors.length > 0 ? "yellow" : "green",
      });

      modals.closeSelf();
    } catch (error) {
      notifications.show({
        title: `${label} Failed`,
        message: String(error),
        color: "red",
      });
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        handleSubmit();
      }}
    >
      <Stack>
        <Text size="sm">
          Apply <strong>{label}</strong> to <strong>{items.length}</strong>{" "}
          selected item(s)?
        </Text>

        <Divider />

        <Group justify="space-between">
          <Button variant="default" onClick={() => modals.closeSelf()}>
            Cancel
          </Button>
          <Button
            type="submit"
            loading={isPending}
            disabled={items.length === 0}
          >
            Apply to {items.length} Item(s)
          </Button>
        </Group>
      </Stack>
    </form>
  );
};

export const BatchModConfirmModal = withModal(
  BatchModConfirmForm,
  "batch-mod-confirm",
  {
    title: "Confirm Batch Operation",
    size: "md",
  },
);

export default BatchModConfirmForm;
