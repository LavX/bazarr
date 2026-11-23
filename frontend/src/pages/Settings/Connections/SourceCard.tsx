import {
  ActionIcon,
  Badge,
  Button,
  Group,
  Menu,
  Stack,
  Text,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import {
  faEllipsisVertical,
  faPen,
  faPlus,
  faRotateRight,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  mediaSourceErrorMessage,
  useScanMediaSource,
} from "@/apis/hooks/mediaSources";
import type { MediaSource, SourceLibrary } from "@/apis/raw/mediaSources";
import LibraryRows from "./LibraryRows";
import type { SourceEditor } from "./SourceFormModal";
import styles from "./Connections.module.scss";

export default function SourceCard({
  source,
  profileNames,
  onEdit,
  onRemove,
}: {
  source: MediaSource;
  profileNames: ReadonlyMap<number, string>;
  onEdit: (editor: SourceEditor) => void;
  onRemove: (source: MediaSource, library?: SourceLibrary) => void;
}) {
  const scan = useScanMediaSource();
  const canScan =
    source.enabled && source.libraries.some((library) => library.enabled);
  const hours = source.scan_frequency_hours;
  const runScan = () => {
    scan.mutate(source, {
      onSuccess: (jobId) =>
        showNotification({
          color: jobId === null ? "yellow" : "green",
          message:
            jobId === null
              ? "No new folder scan was queued."
              : "Folder scan queued.",
        }),
      onError: (error) =>
        showNotification({
          color: "red",
          message: mediaSourceErrorMessage(
            error,
            "Could not queue the folder scan.",
          ),
        }),
    });
  };

  return (
    <article className={styles.card} aria-label={source.name}>
      <Stack gap="sm">
        <div className={styles.cardLayout}>
          <Stack gap={4}>
            <Group gap="xs">
              <Text
                component="h3"
                fw={600}
                style={{ overflowWrap: "anywhere" }}
              >
                {source.name}
              </Text>
              <Badge
                size="sm"
                color={source.enabled ? "green" : "gray"}
                variant="light"
              >
                {source.enabled ? "Enabled" : "Disabled"}
              </Badge>
            </Group>
            <Text size="sm" c="dimmed">
              {hours === 0
                ? "Manual scans only"
                : `Scan every ${hours} ${hours === 1 ? "hour" : "hours"}`}
            </Text>
          </Stack>
          <Group gap="xs" className={styles.cardActions}>
            <Button
              type="button"
              size="xs"
              variant="light"
              leftSection={<FontAwesomeIcon icon={faRotateRight} />}
              disabled={!canScan}
              loading={scan.isPending}
              onClick={runScan}
            >
              Scan now
            </Button>
            <Button
              type="button"
              size="xs"
              variant="default"
              leftSection={<FontAwesomeIcon icon={faPen} />}
              onClick={() => onEdit({ mode: "edit-source", source })}
            >
              Edit source
            </Button>
            <Menu position="bottom-end" withArrow>
              <Menu.Target>
                <ActionIcon
                  type="button"
                  variant="subtle"
                  color="gray"
                  aria-label={`More actions for ${source.name}`}
                >
                  <FontAwesomeIcon icon={faEllipsisVertical} />
                </ActionIcon>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  color="red"
                  leftSection={<FontAwesomeIcon icon={faTrash} />}
                  onClick={() => onRemove(source)}
                >
                  Remove source
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          </Group>
        </div>
        <Group justify="space-between" mt="xs">
          <Text size="sm" fw={600}>
            Libraries
          </Text>
          <Button
            type="button"
            size="compact-xs"
            variant="subtle"
            leftSection={<FontAwesomeIcon icon={faPlus} />}
            onClick={() => onEdit({ mode: "add-library", source })}
          >
            Add library
          </Button>
        </Group>
        <LibraryRows
          libraries={source.libraries}
          profileNames={profileNames}
          onEdit={(library) =>
            onEdit({ mode: "edit-library", source, library })
          }
          onRemove={(library) => onRemove(source, library)}
        />
      </Stack>
    </article>
  );
}
