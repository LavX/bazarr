import { FunctionComponent } from "react";
import {
  Button,
  Checkbox,
  Divider,
  Group,
  NumberInput,
  Stack,
  Text,
} from "@mantine/core";
import { useForm } from "@mantine/form";
import { notifications } from "@mantine/notifications";
import { useBatchAction, useSystemSettings } from "@/apis/hooks";
import { BatchItem, BatchOptions } from "@/apis/raw/subtitles";
import { useModals, withModal } from "@/modules/modals";

interface MassSyncFormProps {
  items: BatchItem[];
}

const MassSyncForm: FunctionComponent<MassSyncFormProps> = ({ items }) => {
  const { data: settings } = useSystemSettings();
  const { mutateAsync, isPending } = useBatchAction();
  const modals = useModals();

  const form = useForm<BatchOptions>({
    initialValues: {
      maxOffsetSeconds: settings?.subsync.max_offset_seconds ?? 60,
      noFixFramerate: settings?.subsync.no_fix_framerate ?? true,
      gss: settings?.subsync.gss ?? true,
      forceResync: false,
    },
  });

  const handleSubmit = form.onSubmit(async (values) => {
    if (items.length >= 100) {
      const confirmed = window.confirm(
        `This will sync subtitles for ${items.length} items. This may take a while. Continue?`,
      );
      if (!confirmed) return;
    }

    try {
      const result = await mutateAsync({
        items,
        action: "sync",
        options: values,
      });

      notifications.show({
        title: "Mass Sync Queued",
        message: `Queued: ${result.queued}, Skipped: ${result.skipped}${result.errors.length > 0 ? `, Errors: ${result.errors.length}` : ""}`,
        color: result.errors.length > 0 ? "yellow" : "green",
      });

      modals.closeSelf();
    } catch (error) {
      notifications.show({
        title: "Mass Sync Failed",
        message: String(error),
        color: "red",
      });
    }
  });

  return (
    <form onSubmit={handleSubmit}>
      <Stack>
        <Text size="sm">
          Sync subtitles for <strong>{items.length}</strong> selected item(s).
          Subtitles will be synchronized using the video audio track as
          reference.
        </Text>

        <NumberInput
          label="Max Offset Seconds"
          min={1}
          max={600}
          {...form.getInputProps("maxOffsetSeconds")}
        />

        <Checkbox
          label="Golden-Section Search"
          {...form.getInputProps("gss", { type: "checkbox" })}
        />

        <Checkbox
          label="No Fix Framerate"
          {...form.getInputProps("noFixFramerate", { type: "checkbox" })}
        />

        <Checkbox
          label="Force Re-sync"
          description="Re-sync subtitles that have already been synced"
          {...form.getInputProps("forceResync", { type: "checkbox" })}
        />

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
            Sync {items.length} Item(s)
          </Button>
        </Group>
      </Stack>
    </form>
  );
};

export const MassSyncModal = withModal(MassSyncForm, "mass-sync", {
  title: "Mass Sync Subtitles",
  size: "md",
});

export default MassSyncForm;
