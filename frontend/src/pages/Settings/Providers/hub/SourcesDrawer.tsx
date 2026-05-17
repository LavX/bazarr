import { FunctionComponent, useEffect, useState } from "react";
import {
  ActionIcon,
  Anchor,
  Button,
  Drawer,
  Group,
  Menu,
  Stack,
  Text,
  TextInput,
} from "@mantine/core";
import {
  faEllipsis,
  faPlus,
  faShieldHalved,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  useProviderHubAddCatalogSource,
  useProviderHubRefreshCatalog,
  useProviderHubRemoveCatalogSource,
} from "@/apis/hooks";
import type { ProviderHubCatalogSource } from "@/apis/raw/providerHub";
import { EmptyState } from "@/pages/Settings/Providers/hub/components/EmptyState";
import { TrustBadge } from "@/pages/Settings/Providers/hub/components/TrustBadge";
import {
  formatRelativeTime,
  parseGitHubUrl,
} from "@/pages/Settings/Providers/hub/utils";
import styles from "@/pages/Settings/Providers/hub/hub.module.scss";

interface SourcesDrawerProps {
  opened: boolean;
  onClose: () => void;
  sources: ProviderHubCatalogSource[];
  installedSourceUsage: Record<string, number>;
}

export const SourcesDrawer: FunctionComponent<SourcesDrawerProps> = ({
  opened,
  onClose,
  sources,
  installedSourceUsage,
}) => {
  const [name, setName] = useState("");
  const [url, setUrl] = useState("");
  const [nameTouched, setNameTouched] = useState(false);

  const addSource = useProviderHubAddCatalogSource();
  const removeSource = useProviderHubRemoveCatalogSource();
  const refresh = useProviderHubRefreshCatalog();

  useEffect(() => {
    if (!nameTouched) {
      const parsed = parseGitHubUrl(url);
      if (parsed) setName(parsed.suggestedName);
      else if (!url) setName("");
    }
  }, [url, nameTouched]);

  const canAdd = name.trim().length > 0 && url.trim().length > 0;

  const handleAdd = () => {
    if (!canAdd) return;
    addSource.mutate(
      { name: name.trim(), url: url.trim() },
      {
        onSuccess: () => {
          setName("");
          setUrl("");
          setNameTouched(false);
        },
      },
    );
  };

  const handleRemove = (source: ProviderHubCatalogSource) => {
    const count = installedSourceUsage[source.name] ?? 0;
    const msg =
      count > 0
        ? `Remove "${source.name}"? ${count} installed provider${count === 1 ? " stays" : "s stay"} but will no longer receive updates from this source.`
        : `Remove "${source.name}"?`;
    if (typeof window !== "undefined" && window.confirm(msg)) {
      removeSource.mutate(source.name);
    }
  };

  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size="md"
      title={
        <Text size="lg" fw={600}>
          Catalog sources
        </Text>
      }
      padding="md"
    >
      <Stack gap="lg">
        <Stack gap="xs">
          <Text size="sm" c="dimmed">
            Add a GitHub-hosted catalog JSON to discover community-maintained
            providers. We auto-fill the Name from the repo when possible.
          </Text>
          <TextInput
            label="GitHub catalog URL"
            placeholder="https://github.com/owner/repo/blob/main/catalog.json"
            value={url}
            onChange={(e) => setUrl(e.currentTarget.value)}
          />
          <TextInput
            label="Name"
            value={name}
            onChange={(e) => {
              setNameTouched(true);
              setName(e.currentTarget.value);
            }}
          />
          <Button
            onClick={handleAdd}
            disabled={!canAdd}
            loading={addSource.isPending}
            leftSection={<FontAwesomeIcon icon={faPlus} />}
            variant="light"
            mt="xs"
          >
            Add source
          </Button>
        </Stack>

        <Stack gap="xs">
          <Group justify="space-between">
            <Text size="sm" fw={600}>
              Configured sources
            </Text>
            <Button
              size="xs"
              variant="subtle"
              onClick={() => refresh.mutate()}
              loading={refresh.isPending}
            >
              Refresh all
            </Button>
          </Group>
          {sources.length === 0 ? (
            <EmptyState
              icon={faShieldHalved}
              title="No catalog sources yet"
              body="Add your first GitHub catalog above to start browsing community providers."
            />
          ) : (
            <Stack gap="xs">
              {sources.map((source) => {
                const usage = installedSourceUsage[source.name] ?? 0;
                return (
                  <div
                    key={source.id ?? source.name}
                    className={styles.sourceCard}
                  >
                    <div className={styles.sourceCardHeader}>
                      <div style={{ minWidth: 0 }}>
                        <div className={styles.sourceCardName}>
                          {source.name}
                        </div>
                        <Anchor
                          href={source.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className={styles.sourceCardMeta}
                        >
                          {source.url}
                        </Anchor>
                      </div>
                      <Menu position="bottom-end" withinPortal>
                        <Menu.Target>
                          <ActionIcon
                            variant="subtle"
                            color="gray"
                            aria-label={`Actions for ${source.name}`}
                          >
                            <FontAwesomeIcon icon={faEllipsis} />
                          </ActionIcon>
                        </Menu.Target>
                        <Menu.Dropdown>
                          <Menu.Item
                            color="red"
                            leftSection={<FontAwesomeIcon icon={faTrash} />}
                            onClick={() => handleRemove(source)}
                          >
                            Remove source
                          </Menu.Item>
                        </Menu.Dropdown>
                      </Menu>
                    </div>
                    <Group justify="space-between" align="center">
                      <Group gap={6}>
                        <TrustBadge trusted={source.trusted} />
                      </Group>
                      <Group gap={12}>
                        {usage > 0 && (
                          <Text size="xs" c="dimmed">
                            {usage} installed
                          </Text>
                        )}
                        {source.last_checked_at && (
                          <Text size="xs" c="dimmed">
                            Checked {formatRelativeTime(source.last_checked_at)}
                          </Text>
                        )}
                      </Group>
                    </Group>
                    {source.last_error && (
                      <Text size="xs" c="red">
                        {source.last_error}
                      </Text>
                    )}
                  </div>
                );
              })}
            </Stack>
          )}
        </Stack>

        <Stack gap="xs">
          <Text size="sm" fw={600}>
            About trust
          </Text>
          <Text size="xs" c="dimmed">
            A &quot;Trusted&quot; badge means the source is on your trusted
            list. It says nothing about plugin quality. Community sources are
            useful but you should review the manifest before installing.
          </Text>
        </Stack>
      </Stack>
    </Drawer>
  );
};
