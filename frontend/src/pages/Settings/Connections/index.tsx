import { FunctionComponent, ReactNode, useMemo, useState } from "react";
import { useLocation, useNavigate } from "react-router";
import {
  Alert,
  Button,
  Group,
  List,
  Modal,
  Skeleton,
  Stack,
  Tabs,
  Text,
  ThemeIcon,
} from "@mantine/core";
import { showNotification } from "@mantine/notifications";
import {
  faPaperPlane,
  faPlus,
  faRotateRight,
  faServer,
  faTriangleExclamation,
  faTv,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import {
  getArrInstanceDeleteConflict,
  getArrInstanceErrorMessage,
  useArrInstances,
  useDeleteArrInstance,
} from "@/apis/hooks";
import type {
  ArrInstance,
  ArrInstanceDeleteConflict,
  ArrInstanceLibrary,
  ArrKind,
} from "@/apis/raw/arrInstances";
import { Layout, Section } from "@/pages/Settings/components";
import MediaServerSection from "@/pages/Settings/MediaServers/MediaServerSection";
import PlexAccountSection from "@/pages/Settings/Plex/PlexAccountSection";
import RadarrSection from "@/pages/Settings/Radarr/RadarrSection";
import SeerrSection from "@/pages/Settings/Seerr/SeerrSection";
import SonarrSection from "@/pages/Settings/Sonarr/SonarrSection";
import SportarrSection from "@/pages/Settings/Sportarr/SportarrSection";
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

function plural(count: number, one: string, many: string) {
  return `${count} ${count === 1 ? one : many}`;
}

// What deleting an instance with its library removes, in the words the rest of
// the UI uses. Empty categories are left out.
function libraryItems(library: ArrInstanceLibrary) {
  return [
    plural(library.series, "series", "series"),
    plural(library.episodes, "episode", "episodes"),
    plural(library.movies, "movie", "movies"),
    plural(library.history, "history entry", "history entries"),
    plural(library.blacklist, "exclusion record", "exclusion records"),
    plural(library.root_folders, "root folder record", "root folder records"),
  ].filter((item) => !item.startsWith("0 "));
}

function librarySummary(library: ArrInstanceLibrary) {
  const items = libraryItems(library);
  if (items.length < 2) {
    return items[0] ?? "records";
  }
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

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
  // Why the server refused the last delete, when it did.
  const [refusal, setRefusal] = useState<ArrInstanceDeleteConflict | null>(
    null,
  );
  // The refusal of an instance that owns a synced library. It opens the way to
  // deleting the instance together with that library, as a second step.
  const [ownedLibrary, setOwnedLibrary] =
    useState<ArrInstanceDeleteConflict | null>(null);
  const [confirmLibrary, setConfirmLibrary] = useState(false);

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
    setRefusal(null);
    setOwnedLibrary(null);
    setConfirmLibrary(false);
    setDeleteOpened(true);
  };

  // Deleting the only Sonarr or Radarr instance switches that kind off on the
  // server, whether or not its library goes too.
  const lastOfKind =
    deleteTarget !== null &&
    deleteTarget.kind !== "sportarr" &&
    (instances.data ?? []).filter(
      (instance) => instance.kind === deleteTarget.kind,
    ).length === 1;

  const confirmDelete = (removeLibrary: boolean) => {
    if (!deleteTarget) {
      return;
    }
    const removedName = deleteTarget.name;
    setRefusal(null);
    deleteInstance.mutate(
      { id: deleteTarget.id, removeLibrary },
      {
        onSuccess: () => {
          // Nothing about the kind switching off: the dialog said it would,
          // and the switch, read again from the server, shows whether it did.
          showNotification({
            color: "green",
            message: removeLibrary
              ? `Instance "${removedName}" and its synced library removed`
              : `Instance "${removedName}" removed`,
          });
          setDeleteOpened(false);
        },
        onError: (error) => {
          const conflict = getArrInstanceDeleteConflict(error);
          if (!conflict) {
            setDeleteOpened(false);
          } else if (conflict.can_remove_library && conflict.library) {
            setOwnedLibrary(conflict);
          } else {
            setRefusal({
              ...conflict,
              message: getArrInstanceErrorMessage(
                error,
                "Bazarr+ could not delete this instance.",
              ),
            });
          }
        },
      },
    );
  };

  const kindLabel = deleteTarget ? ARR_META[deleteTarget.kind].label : "";
  const libraryStep = confirmLibrary && ownedLibrary?.library;
  // Adding an instance later does not switch the kind back on by itself.
  const switchOffNote = `This is your last ${kindLabel} instance, so Use ${kindLabel} is switched off too. Switch it back on when you add a new instance.`;

  const refusalAlert = refusal && (
    <Alert
      color="yellow"
      icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
      title={
        refusal.error === "sync_in_progress"
          ? "Library sync in progress"
          : refusal.error === "job_in_progress"
            ? "Subtitle job in progress"
            : "Instance could not be deleted"
      }
    >
      {refusal.message}
    </Alert>
  );

  return (
    <Layout name="Connections">
      <Tabs value={activeTab} onChange={handleTabChange} keepMounted={false}>
        <Tabs.List mb="md" className={styles.tabList}>
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
            value="sportarr"
            leftSection={<FontAwesomeIcon icon={ARR_META.sportarr.icon} />}
          >
            Sportarr
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
          <Tabs.Tab value="emby" leftSection={<FontAwesomeIcon icon={faTv} />}>
            Emby
          </Tabs.Tab>
          <Tabs.Tab
            value="silo"
            leftSection={<FontAwesomeIcon icon={faServer} />}
          >
            Silo
          </Tabs.Tab>
          <Tabs.Tab
            value="seerr"
            leftSection={<FontAwesomeIcon icon={faPaperPlane} />}
          >
            Seerr
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

        <Tabs.Panel value="sportarr">
          <SportarrSection>
            <KindSection
              kind="sportarr"
              query={instances}
              onAdd={openCreate}
              onEdit={openEdit}
              onDelete={openDelete}
            />
          </SportarrSection>
        </Tabs.Panel>

        <Tabs.Panel value="plex">
          <MediaServerSection kind="plex" />
          <PlexAccountSection />
        </Tabs.Panel>

        <Tabs.Panel value="jellyfin">
          <MediaServerSection kind="jellyfin" />
        </Tabs.Panel>
        <Tabs.Panel value="emby">
          <MediaServerSection kind="emby" />
        </Tabs.Panel>
        <Tabs.Panel value="silo">
          <MediaServerSection kind="silo" />
        </Tabs.Panel>
        <Tabs.Panel value="seerr">
          <SeerrSection />
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
        title={
          libraryStep ? "Delete instance and its library" : "Delete instance"
        }
        centered
      >
        {libraryStep ? (
          <Stack gap="md">
            <Text size="sm">
              Delete{" "}
              <Text span fw={600}>
                {deleteTarget?.name}
              </Text>{" "}
              ({kindLabel}){" "}
              {ownedLibrary?.last_of_kind
                ? `together with every ${kindLabel} record Bazarr+ holds:`
                : "together with everything Bazarr+ synced from it:"}
            </Text>
            <List size="sm">
              {libraryItems(libraryStep).map((item) => (
                <List.Item key={item}>{item}</List.Item>
              ))}
            </List>
            <Text size="sm">
              {`These are removed from the Bazarr+ database only. Video and subtitle files on disk are not touched, and nothing changes in ${kindLabel} itself.`}
            </Text>
            {ownedLibrary?.last_of_kind && (
              <Text size="sm">{switchOffNote}</Text>
            )}
            <Text size="sm" fw={600}>
              This cannot be undone.
            </Text>
            {refusalAlert}
            <Group justify="flex-end">
              <Button
                type="button"
                variant="default"
                onClick={() => {
                  setRefusal(null);
                  setConfirmLibrary(false);
                }}
              >
                Back
              </Button>
              <Button
                type="button"
                color="red"
                loading={deleteInstance.isPending}
                onClick={() => confirmDelete(true)}
              >
                Delete instance and library
              </Button>
            </Group>
          </Stack>
        ) : (
          <Stack gap="md">
            <Text size="sm">
              Delete{" "}
              <Text span fw={600}>
                {deleteTarget?.name}
              </Text>{" "}
              ({kindLabel})?{" "}
              {deleteTarget?.kind === "sportarr"
                ? "Its connection settings and owned sports library, history and exclusion records will be removed. Media and subtitle files remain on disk. This cannot be undone."
                : "Its connection settings will be removed. This cannot be undone."}
            </Text>
            {lastOfKind && <Text size="sm">{switchOffNote}</Text>}
            {ownedLibrary?.library && (
              <Alert
                color="yellow"
                icon={<FontAwesomeIcon icon={faTriangleExclamation} />}
                title="This instance still has a synced library"
              >
                <Stack gap="xs">
                  <span>
                    {ownedLibrary.last_of_kind
                      ? `Bazarr+ still holds ${librarySummary(ownedLibrary.library)} from ${kindLabel}, so its last instance cannot be deleted on its own.`
                      : `Bazarr+ still holds ${librarySummary(ownedLibrary.library)} synced from it, so it cannot be deleted on its own.`}
                  </span>
                  <span>
                    You can delete it together with that library. Video and
                    subtitle files on disk are not touched.
                  </span>
                  <Group>
                    <Button
                      type="button"
                      size="xs"
                      variant="light"
                      color="red"
                      onClick={() => {
                        setRefusal(null);
                        setConfirmLibrary(true);
                      }}
                    >
                      Delete with its synced library
                    </Button>
                  </Group>
                </Stack>
              </Alert>
            )}
            {refusalAlert}
            <Group justify="flex-end">
              <Button
                type="button"
                variant="default"
                onClick={() => setDeleteOpened(false)}
              >
                Cancel
              </Button>
              {!ownedLibrary && (
                <Button
                  type="button"
                  color="red"
                  loading={deleteInstance.isPending}
                  onClick={() => confirmDelete(false)}
                >
                  Delete instance
                </Button>
              )}
            </Group>
          </Stack>
        )}
      </Modal>
    </Layout>
  );
};

export default SettingsConnectionsView;
