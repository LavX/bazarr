import {
  ActionIcon,
  Badge,
  Divider,
  Group,
  Menu,
  Stack,
  Text,
} from "@mantine/core";
import {
  faEllipsisVertical,
  faPen,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import type { SourceContentKind, SourceLibrary } from "@/apis/raw/mediaSources";

export const CONTENT_KIND_LABELS: Record<SourceContentKind, string> = {
  movie: "Movies",
  series: "Series",
  sports: "Sports",
};

const SCAN_STATUS: Record<string, { label: string; color: string }> = {
  ok: { label: "Scan complete", color: "green" },
  partial: { label: "Partial scan", color: "yellow" },
  root_missing: { label: "Folder unavailable", color: "yellow" },
  error: { label: "Scan failed", color: "red" },
};

export default function LibraryRows({
  libraries,
  profileNames,
  onEdit,
  onRemove,
}: {
  libraries: SourceLibrary[];
  profileNames: ReadonlyMap<number, string>;
  onEdit: (library: SourceLibrary) => void;
  onRemove: (library: SourceLibrary) => void;
}) {
  if (libraries.length === 0) {
    return (
      <Text size="sm" c="dimmed">
        No libraries yet.
      </Text>
    );
  }

  return (
    <Stack gap="xs">
      {libraries.map((library) => {
        const status = SCAN_STATUS[library.last_scan_status ?? ""];
        const profile =
          library.profile_id === null
            ? "No profile"
            : (profileNames.get(library.profile_id) ??
              `Profile ${library.profile_id}`);
        return (
          <Stack key={library.id} gap="xs">
            <Divider />
            <Group align="flex-start" wrap="nowrap" justify="space-between">
              <Stack gap={4} miw={0}>
                <Text size="sm" fw={600} style={{ overflowWrap: "anywhere" }}>
                  {library.locator}
                </Text>
                <Group gap="xs">
                  <Text size="xs" c="dimmed">
                    {CONTENT_KIND_LABELS[library.content_kind]}
                  </Text>
                  <Text size="xs" c="dimmed">
                    {profile}
                  </Text>
                  <Badge
                    size="xs"
                    variant="light"
                    color={library.enabled ? "green" : "gray"}
                  >
                    {library.enabled ? "Enabled" : "Disabled"}
                  </Badge>
                </Group>
                <Group role="status" gap="xs">
                  <Text fw={500} size="xs" c={status?.color ?? "dimmed"}>
                    {status?.label ??
                      library.last_scan_status ??
                      "Never scanned"}
                  </Text>
                  {library.last_scan_message && (
                    <Text
                      size="xs"
                      c="dimmed"
                      style={{ overflowWrap: "anywhere" }}
                    >
                      {library.last_scan_message}
                    </Text>
                  )}
                </Group>
              </Stack>
              <Menu position="bottom-end" withArrow>
                <Menu.Target>
                  <ActionIcon
                    type="button"
                    variant="subtle"
                    color="gray"
                    aria-label={`Actions for library ${library.locator}`}
                  >
                    <FontAwesomeIcon icon={faEllipsisVertical} />
                  </ActionIcon>
                </Menu.Target>
                <Menu.Dropdown>
                  <Menu.Item
                    leftSection={<FontAwesomeIcon icon={faPen} />}
                    onClick={() => onEdit(library)}
                  >
                    Edit library
                  </Menu.Item>
                  <Menu.Item
                    color="red"
                    leftSection={<FontAwesomeIcon icon={faTrash} />}
                    onClick={() => onRemove(library)}
                  >
                    Remove library
                  </Menu.Item>
                </Menu.Dropdown>
              </Menu>
            </Group>
          </Stack>
        );
      })}
    </Stack>
  );
}
