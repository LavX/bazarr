import { Link } from "react-router";
import { ActionIcon, Group, Menu, Tooltip } from "@mantine/core";
import {
  faCloudUploadAlt,
  faEllipsisVertical,
  faHardDrive,
  faMagnifyingGlass,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { refreshWhenJobFinishes } from "@/apis/hooks/jobWatch";
import { QueryKeys } from "@/apis/queries/keys";
import api from "@/apis/raw";
import { libraryItemPath, LibraryKind } from "@/apis/raw/library";
import { Toolbox } from "@/components";
import { useModals } from "@/modules/modals";
import { sourceDetailApi, sourceDetailKeys } from "@/pages/SourceDetail/api";
import {
  SourceManualSearchModal,
  SourceUploadModal,
} from "@/pages/SourceDetail/forms";

interface SourceActionRow {
  origin: "source";
  id: number;
  source_id: number | null;
  library_id: number;
  profileId: number | null;
  identity_state: string;
  shadowed_by: string | null;
  path: string | null;
  sceneName?: string | null;
}

type CanUseLibrary = (row: SourceActionRow) => boolean;
type SourceAction = "search" | "scan";

function canSearch(row: SourceActionRow, kind: LibraryKind) {
  return (
    kind === "series" ||
    (row.profileId != null &&
      !row.shadowed_by &&
      (row.identity_state === "identified" || row.identity_state === "parsed"))
  );
}

function useLibrarySourceAction(kind: LibraryKind) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: async ({
      action,
      rows,
    }: {
      action: SourceAction;
      rows: SourceActionRow[];
    }) => {
      const scanTargets = new Map<
        string,
        { sourceId: number; libraryId: number }
      >();
      const refresh = async () => {
        await Promise.all([
          client.invalidateQueries({ queryKey: [QueryKeys.Series, "library"] }),
          client.invalidateQueries({ queryKey: [QueryKeys.Movies, "library"] }),
          client.invalidateQueries({ queryKey: sourceDetailKeys.root }),
          client.invalidateQueries({
            queryKey: [QueryKeys.System, QueryKeys.Badges],
          }),
        ]);
      };
      const search =
        kind === "series"
          ? (id: number) => sourceDetailApi.searchGroup(id)
          : (id: number) => sourceDetailApi.search(id);
      for (const row of rows) {
        if (action === "search") {
          const job = await search(row.id);
          await refreshWhenJobFinishes(client, job.job_id, refresh);
        } else if (row.source_id != null) {
          scanTargets.set(`${row.source_id}:${row.library_id}`, {
            sourceId: row.source_id,
            libraryId: row.library_id,
          });
        }
      }
      for (const { sourceId, libraryId } of scanTargets.values()) {
        const jobId = await api.mediaSources.scanLibrary(sourceId, libraryId);
        await refreshWhenJobFinishes(client, jobId, refresh);
      }
    },
  });
}

export function LibrarySourceRowActions({
  kind,
  row,
  canUseLibrary,
}: {
  kind: LibraryKind;
  row: SourceActionRow;
  canUseLibrary: CanUseLibrary;
}) {
  const action = useLibrarySourceAction(kind);
  const modals = useModals();
  const active = canUseLibrary(row);
  const searchable = active && canSearch(row, kind);
  return (
    <Menu shadow="md" width={220} position="bottom-end">
      <Menu.Target>
        <Tooltip label="Actions">
          <ActionIcon
            aria-label="Actions"
            variant="subtle"
            size="sm"
            loading={action.isPending}
          >
            <FontAwesomeIcon icon={faEllipsisVertical} />
          </ActionIcon>
        </Tooltip>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Item
          component={Link}
          to={libraryItemPath(kind, row)}
          leftSection={<FontAwesomeIcon icon={faWrench} size="sm" />}
        >
          Edit
        </Menu.Item>
        <Menu.Item
          disabled={!searchable}
          onClick={() => action.mutate({ action: "search", rows: [row] })}
          leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />}
        >
          Search Missing
        </Menu.Item>
        {kind === "movies" && (
          <>
            <Menu.Item
              disabled={!searchable || !row.path}
              onClick={() => {
                if (!row.path) return;
                modals.openContextModal(SourceManualSearchModal, {
                  item: {
                    id: row.id,
                    path: row.path,
                    scene_name: row.sceneName ?? null,
                  },
                });
              }}
              leftSection={
                <FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />
              }
            >
              Manual Search
            </Menu.Item>
            <Menu.Item
              disabled={!searchable}
              onClick={() =>
                modals.openContextModal(SourceUploadModal, {
                  item: { id: row.id },
                })
              }
              leftSection={
                <FontAwesomeIcon icon={faCloudUploadAlt} size="sm" />
              }
            >
              Upload
            </Menu.Item>
          </>
        )}
        <Menu.Item
          disabled={!active}
          onClick={() => action.mutate({ action: "scan", rows: [row] })}
          leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
        >
          Scan Disk
        </Menu.Item>
      </Menu.Dropdown>
    </Menu>
  );
}

export function LibrarySourceSelectionActions({
  kind,
  rows,
  canUseLibrary,
}: {
  kind: LibraryKind;
  rows: SourceActionRow[];
  canUseLibrary: CanUseLibrary;
}) {
  const action = useLibrarySourceAction(kind);
  const active = rows.every(canUseLibrary);
  return (
    <Group gap="xs">
      <Toolbox.Button
        icon={faMagnifyingGlass}
        disabled={!active || !rows.every((row) => canSearch(row, kind))}
        loading={action.isPending}
        onClick={() => action.mutate({ action: "search", rows })}
      >
        Search Missing
      </Toolbox.Button>
      <Toolbox.Button
        icon={faHardDrive}
        disabled={!active}
        loading={action.isPending}
        onClick={() => action.mutate({ action: "scan", rows })}
      >
        Scan Disk
      </Toolbox.Button>
    </Group>
  );
}
