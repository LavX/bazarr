import { useState } from "react";
import {
  Alert,
  Button,
  Group,
  Modal,
  Skeleton,
  Stack,
  Text,
} from "@mantine/core";
import {
  useDeleteMediaServerInstance,
  useMediaServerInstances,
} from "@/apis/hooks/mediaServers";
import type {
  MediaServerInstance,
  MediaServerKind,
} from "@/apis/raw/mediaServers";
import { Check, Section } from "@/pages/Settings/components";
import { useStagedValues } from "@/pages/Settings/utilities/FormValues";
import { useSettingValue } from "@/pages/Settings/utilities/hooks";
import MediaServerInstanceCard from "./MediaServerInstanceCard";
import MediaServerInstanceFormModal from "./MediaServerInstanceFormModal";
import styles from "@/pages/Settings/Connections/Connections.module.scss";

function DeleteInstanceModal({
  instance,
  onClose,
}: {
  instance: MediaServerInstance;
  onClose: () => void;
}) {
  const remove = useDeleteMediaServerInstance(instance.kind, instance.id);
  return (
    <Modal opened onClose={onClose} title="Delete instance" centered>
      <Stack gap="md">
        <Text size="sm">
          Delete {instance.name}? Its connection settings and pending refreshes
          will be removed. This cannot be undone.
        </Text>
        {remove.isError && (
          <Alert color="red">
            Could not delete this instance. Check that the Bazarr API is
            available and try again.
          </Alert>
        )}
        <Group justify="flex-end">
          <Button type="button" variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            type="button"
            color="red"
            loading={remove.isPending}
            onClick={() => remove.mutate(undefined, { onSuccess: onClose })}
          >
            Delete instance
          </Button>
        </Group>
      </Stack>
    </Modal>
  );
}

export default function MediaServerSection({
  kind,
}: {
  kind: MediaServerKind;
}) {
  const name = kind === "emby" ? "Emby" : "Silo";
  const enabledKey = `settings-general-use_${kind}`;
  const savedEnabled =
    useSettingValue<boolean>(enabledKey, { original: true }) ?? false;
  const staged = useStagedValues();
  const masterChanged = Object.hasOwn(staged, enabledKey);
  const query = useMediaServerInstances(kind);
  const [editor, setEditor] = useState<{
    instance: MediaServerInstance | null;
  } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<MediaServerInstance | null>(
    null,
  );
  return (
    <>
      <Section header={`Use ${name}`}>
        <Check label="Enabled" settingKey={enabledKey} />
        <Text size="sm" c="dimmed">
          {kind === "emby"
            ? "Notify Emby about movie subtitle additions."
            : "Notify Silo about movie and episode subtitle changes. Subtitle sidecars must be beside the video file."}
        </Text>
      </Section>
      <Section header={`${name} instances`}>
        <Group justify="space-between">
          <Text size="sm" c="dimmed">
            Manage each {name} connection and its refreshes independently.
          </Text>
          <Button
            type="button"
            size="xs"
            variant="light"
            onClick={() => setEditor({ instance: null })}
          >
            Add instance
          </Button>
        </Group>
        {query.isLoading ? (
          <Skeleton height={96} radius="lg" aria-label="Loading instances" />
        ) : query.isError ? (
          <Alert color="red" title={`Could not load ${name} instances`}>
            <Group justify="space-between">
              <Text size="sm">
                Check that the Bazarr API is reachable, then try again.
              </Text>
              <Button
                type="button"
                size="xs"
                variant="light"
                onClick={() => void query.refetch()}
              >
                Retry
              </Button>
            </Group>
          </Alert>
        ) : query.data?.length === 0 ? (
          <div className={styles.empty}>
            <Text fw={600}>No {name} instances yet</Text>
            <Text size="sm" c="dimmed">
              Add a connection to choose which local videos notify this server.
            </Text>
          </div>
        ) : (
          <Stack gap="sm">
            {query.data?.map((instance) => (
              <MediaServerInstanceCard
                key={instance.id}
                instance={instance}
                masterEnabled={savedEnabled}
                masterChanged={masterChanged}
                onEdit={(instance) => setEditor({ instance })}
                onDelete={setDeleteTarget}
              />
            ))}
          </Stack>
        )}
      </Section>
      {editor && (
        <MediaServerInstanceFormModal
          key={editor.instance?.id ?? "new"}
          kind={kind}
          instance={editor.instance}
          onClose={() => setEditor(null)}
        />
      )}
      {deleteTarget && (
        <DeleteInstanceModal
          key={deleteTarget.id}
          instance={deleteTarget}
          onClose={() => setDeleteTarget(null)}
        />
      )}
    </>
  );
}
