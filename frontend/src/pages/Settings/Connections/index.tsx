import { FunctionComponent, ReactNode, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import {
  Alert,
  Button,
  Group,
  Modal,
  Skeleton,
  Stack,
  Tabs,
  Text,
  ThemeIcon,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import {
  faPlus,
  faRotateRight,
  faServer,
  faTriangleExclamation,
  faTv,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  getArrInstanceErrorMessage,
  isArrInstanceConflict,
  useArrInstances,
  useDeleteArrInstance,
} from "@/apis/hooks";
import type { ArrInstance, ArrKind } from "@/apis/raw/arrInstances";
import { Layout, Section } from "@/pages/Settings/components";
import JellyfinSection from "@/pages/Settings/Jellyfin/JellyfinSection";
import PlexSection from "@/pages/Settings/Plex/PlexSection";
import RadarrSection from "@/pages/Settings/Radarr/RadarrSection";
import SonarrSection from "@/pages/Settings/Sonarr/SonarrSection";
import InstanceCard from "./InstanceCard";
import InstanceFormModal from "./InstanceFormModal";
import { ARR_META } from "./meta";
import { isConnectionTab, parseTabFromHash } from "./tabs";
import styles from "./Connections.module.scss";

interface KindSectionProps {
  kind: ArrKind;
  query: ReturnType<typeof useArrInstances>;
  onAdd: (kind: ArrKind) => void;
  onEdit: (instance: ArrInstance) => void;
  onDelete: (instance: ArrInstance) => void;
}

const KindSection: FunctionComponent<KindSectionProps> = ({
  kind,
  query,
  onAdd,
  onEdit,
  onDelete,
}) => {
  const meta = ARR_META[kind];

  const list = useMemo(
    () => (query.data ?? []).filter((instance) => instance.kind === kind),
    [query.data, kind],
  );

  let content: ReactNode;
  if (query.isLoading) {
    content = (
      <Stack gap="sm">
        <Skeleton height={96} radius="lg" />
        <Skeleton height={96} radius="lg" />
      </Stack>
    );
  } else if (query.isError) {
    content = (
      <Alert
        color="red"
        icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
        title={`Could not load ${meta.label} instances`}
      >
        <Group justify="space-between" align="center">
          <span>Check that the Bazarr API is reachable, then try again.</span>
          <Button
            size="xs"
            variant="light"
            color="red"
            leftSection={<FontAwesomeIcon icon={faRotateRight} />}
            onClick={() => query.refetch()}
          >
            Retry
          </Button>
        </Group>
      </Alert>
    );
  } else if (list.length === 0) {
    content = (
      <div className={styles.empty}>
        <ThemeIcon variant="light" color="brand" size={46} radius="xl">
          <FontAwesomeIcon icon={meta.icon} />
        </ThemeIcon>
        <Text fw={600}>No {meta.label} instances yet</Text>
        <Text size="sm" c="dimmed" maw={440}>
          Connect one or more {meta.label} servers and manage their {meta.media}{" "}
          side by side. One instance acts as the default for new content.
        </Text>
        <Button
          mt="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faPlus} />}
          onClick={() => onAdd(kind)}
        >
          Add your first {meta.label} instance
        </Button>
      </div>
    );
  } else {
    content = (
      <Stack gap="sm">
        {list.map((instance) => (
          <InstanceCard
            key={instance.id}
            instance={instance}
            onEdit={onEdit}
            onDelete={onDelete}
          />
        ))}
      </Stack>
    );
  }

  return (
    <Section header={meta.label}>
      <Group justify="space-between" align="center">
        <Text size="sm" c="dimmed">
          {meta.label} servers whose {meta.media} Bazarr+ manages subtitles for.
        </Text>
        <Button
          type="button"
          size="xs"
          variant="light"
          leftSection={<FontAwesomeIcon icon={faPlus} />}
          onClick={() => onAdd(kind)}
        >
          Add instance
        </Button>
      </Group>
      {content}
    </Section>
  );
};

const SettingsConnectionsView: FunctionComponent = () => {
  const instances = useArrInstances();
  const deleteInstance = useDeleteArrInstance();

  const location = useLocation();
  const navigate = useNavigate();
  const activeTab = parseTabFromHash(location.hash);
  const handleTabChange = (value: string | null) => {
    if (value && isConnectionTab(value)) {
      navigate({ hash: value }, { replace: true });
    }
  };

  const [editor, setEditor] = useState<{
    kind: ArrKind;
    instance: ArrInstance | null;
  } | null>(null);
  const [editorOpened, setEditorOpened] = useState(false);

  const [deleteTarget, setDeleteTarget] = useState<ArrInstance | null>(null);
  const [deleteOpened, setDeleteOpened] = useState(false);
  const [conflictMessage, setConflictMessage] = useState<string | null>(null);

  const openCreate = (kind: ArrKind) => {
    setEditor({ kind, instance: null });
    setEditorOpened(true);
  };

  const openEdit = (instance: ArrInstance) => {
    setEditor({ kind: instance.kind, instance });
    setEditorOpened(true);
  };

  const openDelete = (instance: ArrInstance) => {
    setDeleteTarget(instance);
    setConflictMessage(null);
    setDeleteOpened(true);
  };

  const confirmDelete = () => {
    if (!deleteTarget) {
      return;
    }
    const removedName = deleteTarget.name;
    deleteInstance.mutate(deleteTarget.id, {
      onSuccess: () => {
        showNotification({
          color: "green",
          message: `Instance "${removedName}" removed`,
        });
        setDeleteOpened(false);
      },
      onError: (error) => {
        if (isArrInstanceConflict(error)) {
          setConflictMessage(
            getArrInstanceErrorMessage(
              error,
              "This instance still owns synced media.",
            ),
          );
        } else {
          setDeleteOpened(false);
        }
      },
    });
  };

  return (
    <Layout name="Connections">
      <Tabs value={activeTab} onChange={handleTabChange} keepMounted={false}>
        <Tabs.List mb="md">
          <Tabs.Tab
            value="sonarr"
            leftSection={<FontAwesomeIcon icon={ARR_META.sonarr.icon} />}
          >
            Sonarr
          </Tabs.Tab>
          <Tabs.Tab
            value="radarr"
            leftSection={<FontAwesomeIcon icon={ARR_META.radarr.icon} />}
          >
            Radarr
          </Tabs.Tab>
          <Tabs.Tab
            value="plex"
            leftSection={<FontAwesomeIcon icon={faServer} />}
          >
            Plex
          </Tabs.Tab>
          <Tabs.Tab
            value="jellyfin"
            leftSection={<FontAwesomeIcon icon={faTv} />}
          >
            Jellyfin
          </Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="sonarr">
          <SonarrSection>
            <KindSection
              kind="sonarr"
              query={instances}
              onAdd={openCreate}
              onEdit={openEdit}
              onDelete={openDelete}
            />
          </SonarrSection>
        </Tabs.Panel>

        <Tabs.Panel value="radarr">
          <RadarrSection>
            <KindSection
              kind="radarr"
              query={instances}
              onAdd={openCreate}
              onEdit={openEdit}
              onDelete={openDelete}
            />
          </RadarrSection>
        </Tabs.Panel>

        <Tabs.Panel value="plex">
          <PlexSection />
        </Tabs.Panel>

        <Tabs.Panel value="jellyfin">
          <JellyfinSection />
        </Tabs.Panel>
      </Tabs>

      <InstanceFormModal
        opened={editorOpened}
        kind={editor?.kind ?? "sonarr"}
        instance={editor?.instance ?? null}
        onClose={() => setEditorOpened(false)}
      />

      <Modal
        opened={deleteOpened}
        onClose={() => setDeleteOpened(false)}
        title="Delete instance"
        centered
      >
        <Stack gap="md">
          <Text size="sm">
            Delete{" "}
            <Text span fw={600}>
              {deleteTarget?.name}
            </Text>{" "}
            ({deleteTarget ? ARR_META[deleteTarget.kind].label : ""})? Its
            connection settings will be removed. This cannot be undone.
          </Text>
          {conflictMessage && (
            <Alert
              color="yellow"
              icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
              title="Instance still in use"
            >
              <Stack gap={4}>
                <span>{conflictMessage}</span>
                <span>
                  Remove or reassign its synced media to another instance, then
                  delete it.
                </span>
              </Stack>
            </Alert>
          )}
          <Group justify="flex-end">
            <Button
              type="button"
              variant="default"
              onClick={() => setDeleteOpened(false)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              color="red"
              loading={deleteInstance.isPending}
              onClick={confirmDelete}
            >
              Delete instance
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Layout>
  );
};

export default SettingsConnectionsView;
