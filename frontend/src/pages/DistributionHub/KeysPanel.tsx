import { FunctionComponent, useState } from "react";
import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Code,
  CopyButton,
  Group,
  Menu,
  Modal,
  Stack,
  Switch,
  Table,
  Text,
  Tooltip,
} from "@mantine/core";
import {
  faCopy,
  faEllipsisVertical,
  faKey,
  faPlus,
  faRotate,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useDistCreateKey,
  useDistDeleteKey,
  useDistKeys,
  useDistProviders,
  useDistRotateKey,
  useDistTiers,
  useDistUpdateKey,
} from "@/apis/hooks";
import type { DistKey } from "@/apis/raw/distributionHub";
import { QueryOverlay } from "@/components/async";
import KeyEditorModal, { KeyDraft } from "./KeyEditorModal";
import { formatLimit, WINDOW_LABELS, WINDOWS } from "./limits";

function draftToBody(draft: KeyDraft) {
  return {
    name: draft.name.trim(),
    tier: draft.tier,
    custom_limits: draft.custom_enabled ? draft.custom_limits : null,
    excluded_providers: draft.excluded_providers.length
      ? draft.excluded_providers
      : null,
    allowed_providers: draft.allowed_providers.length
      ? draft.allowed_providers
      : null,
    timeout_seconds: draft.timeout_seconds,
    note: draft.note?.trim() ? draft.note.trim() : null,
  };
}

const UsageCell: FunctionComponent<{ keyData: DistKey }> = ({ keyData }) => {
  const usage = keyData.usage;
  const limits = keyData.limits;
  if (!usage || !limits) {
    return (
      <Text size="xs" c="dimmed">
        —
      </Text>
    );
  }
  // The cell stays compact (daily figure); hover reveals every window. Lay the
  // two kinds out as side-by-side columns (used / limit) so the popup is wide
  // and short rather than a tall skinny stack.
  const breakdown = (
    <Group align="flex-start" gap="xl" wrap="nowrap">
      {(["search", "download"] as const).map((kind) => (
        <Stack key={kind} gap={2}>
          <Text size="xs" fw={700}>
            {kind === "search" ? "Search" : "Download"}
          </Text>
          {WINDOWS.map((w) => (
            <Text key={w} size="xs" style={{ whiteSpace: "nowrap" }}>
              {WINDOW_LABELS[w]}: {usage[kind][w].toLocaleString()} /{" "}
              {formatLimit(limits[kind][w])}
            </Text>
          ))}
        </Stack>
      ))}
    </Group>
  );
  return (
    <Tooltip label={breakdown} withArrow position="left" w={320} multiline>
      <Stack gap={2} style={{ cursor: "help", width: "fit-content" }}>
        {(["search", "download"] as const).map((kind) => (
          <Text key={kind} size="xs">
            {kind === "search" ? "Search" : "Download"}:{" "}
            {usage[kind].day.toLocaleString()}
            {" / "}
            <Text span c="dimmed">
              {formatLimit(limits[kind].day)} today
            </Text>
          </Text>
        ))}
      </Stack>
    </Tooltip>
  );
};

const TokenRevealModal: FunctionComponent<{
  token: string | null;
  onClose: () => void;
}> = ({ token, onClose }) => (
  <Modal opened={token != null} onClose={onClose} title="Copy your API key">
    <Stack gap="sm">
      <Alert color="yellow">
        This key is shown only once. Copy it now and store it securely. You can
        rotate it later, but it cannot be retrieved again.
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

const KeysPanel: FunctionComponent = () => {
  const keys = useDistKeys();
  const tiers = useDistTiers();
  const providers = useDistProviders();
  const createKey = useDistCreateKey();
  const updateKey = useDistUpdateKey();
  const deleteKey = useDistDeleteKey();
  const rotateKey = useDistRotateKey();

  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<DistKey | null>(null);
  const [revealToken, setRevealToken] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<DistKey | null>(null);

  const openCreate = () => {
    setEditing(null);
    setEditorOpen(true);
  };
  const openEdit = (key: DistKey) => {
    setEditing(key);
    setEditorOpen(true);
  };

  const submit = (draft: KeyDraft) => {
    const body = draftToBody(draft);
    if (editing) {
      updateKey.mutate(
        { id: editing.id, body },
        { onSuccess: () => setEditorOpen(false) },
      );
    } else {
      createKey.mutate(body, {
        onSuccess: (created) => {
          setEditorOpen(false);
          setRevealToken(created.token ?? null);
        },
      });
    }
  };

  const providerNames = providers.data?.providers ?? [];

  return (
    <QueryOverlay result={keys}>
      <Stack>
        <Group justify="space-between">
          <Text c="dimmed" size="sm">
            API keys authorize the OpenSubtitles-compatible endpoint. Send the
            key in the <Code>Api-Key</Code> header.
          </Text>
          <Button
            leftSection={<FontAwesomeIcon icon={faPlus} />}
            onClick={openCreate}
          >
            New key
          </Button>
        </Group>

        <Table.ScrollContainer minWidth={760}>
          <Table verticalSpacing="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Name</Table.Th>
                <Table.Th>Prefix</Table.Th>
                <Table.Th>Tier</Table.Th>
                <Table.Th>Usage</Table.Th>
                <Table.Th>Last used</Table.Th>
                <Table.Th>Enabled</Table.Th>
                <Table.Th />
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {(keys.data?.keys ?? []).map((key) => (
                <Table.Tr key={key.id}>
                  <Table.Td>
                    <Group gap="xs">
                      <FontAwesomeIcon icon={faKey} opacity={0.5} />
                      <span>{key.name}</span>
                      {key.is_legacy === 1 && (
                        <Badge size="xs" variant="light" color="gray">
                          legacy
                        </Badge>
                      )}
                    </Group>
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" ff="monospace" c="dimmed">
                      {key.key_prefix}…
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Badge variant="light">{key.tier_label}</Badge>
                  </Table.Td>
                  <Table.Td>
                    <UsageCell keyData={key} />
                  </Table.Td>
                  <Table.Td>
                    <Text size="xs" c="dimmed">
                      {key.last_used_at
                        ? new Date(key.last_used_at).toLocaleString()
                        : "never"}
                    </Text>
                  </Table.Td>
                  <Table.Td>
                    <Switch
                      checked={key.enabled === 1}
                      onChange={(e) =>
                        updateKey.mutate({
                          id: key.id,
                          body: { enabled: e.currentTarget.checked },
                        })
                      }
                    />
                  </Table.Td>
                  <Table.Td>
                    <Menu position="bottom-end" withinPortal>
                      <Menu.Target>
                        <ActionIcon variant="subtle" color="gray">
                          <FontAwesomeIcon icon={faEllipsisVertical} />
                        </ActionIcon>
                      </Menu.Target>
                      <Menu.Dropdown>
                        <Menu.Item onClick={() => openEdit(key)}>
                          Edit
                        </Menu.Item>
                        {key.is_legacy === 1 ? (
                          // The legacy Default key maps the shared config token:
                          // rotate/delete are handled via Settings -> Regenerate
                          // so the config secret and DB row stay in sync.
                          <Menu.Item disabled>
                            Rotate/delete via Settings → Regenerate
                          </Menu.Item>
                        ) : (
                          <>
                            <Menu.Item
                              leftSection={<FontAwesomeIcon icon={faRotate} />}
                              onClick={() =>
                                rotateKey.mutate(key.id, {
                                  onSuccess: (res) => setRevealToken(res.token),
                                })
                              }
                            >
                              Rotate token
                            </Menu.Item>
                            <Menu.Divider />
                            <Menu.Item
                              color="red"
                              leftSection={<FontAwesomeIcon icon={faTrash} />}
                              onClick={() => setConfirmDelete(key)}
                            >
                              Delete
                            </Menu.Item>
                          </>
                        )}
                      </Menu.Dropdown>
                    </Menu>
                  </Table.Td>
                </Table.Tr>
              ))}
              {keys.data && keys.data.keys.length === 0 && (
                <Table.Tr>
                  <Table.Td colSpan={7}>
                    <Text c="dimmed" ta="center" py="md">
                      No API keys yet. Create one to get started.
                    </Text>
                  </Table.Td>
                </Table.Tr>
              )}
            </Table.Tbody>
          </Table>
        </Table.ScrollContainer>

        <Text size="xs" c="dimmed">
          The usage column shows today&apos;s count; hover it for the full
          hourly / daily / weekly / monthly breakdown.
        </Text>
      </Stack>

      <KeyEditorModal
        opened={editorOpen}
        onClose={() => setEditorOpen(false)}
        editing={editing}
        tiers={tiers.data}
        providers={providerNames}
        saving={createKey.isPending || updateKey.isPending}
        onSubmit={submit}
      />

      <TokenRevealModal
        token={revealToken}
        onClose={() => setRevealToken(null)}
      />

      <Modal
        opened={confirmDelete != null}
        onClose={() => setConfirmDelete(null)}
        title="Delete API key"
      >
        <Stack>
          <Text size="sm">
            Delete <b>{confirmDelete?.name}</b>? Apps using this key will stop
            working immediately. This cannot be undone.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Tooltip
              label="The legacy Default key can't be deleted"
              disabled={confirmDelete?.is_legacy !== 1}
            >
              <Button
                color="red"
                loading={deleteKey.isPending}
                disabled={confirmDelete?.is_legacy === 1}
                onClick={() => {
                  if (confirmDelete) {
                    deleteKey.mutate(confirmDelete.id, {
                      onSettled: () => setConfirmDelete(null),
                    });
                  }
                }}
              >
                Delete
              </Button>
            </Tooltip>
          </Group>
        </Stack>
      </Modal>
    </QueryOverlay>
  );
};

export default KeysPanel;
