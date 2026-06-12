import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import {
  ActionIcon,
  Anchor,
  Badge,
  Checkbox,
  Container,
  Group,
  Menu,
  Progress,
  Text,
  Tooltip,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faArrowUp,
  faBookmark,
  faCheck,
  faCircleDown,
  faEllipsisVertical,
  faHardDrive,
  faLanguage,
  faLayerGroup,
  faMagnifyingGlass,
  faPlay,
  faServer,
  faStop,
  faSync,
  faToolbox,
  faUndo,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { uniqBy } from "lodash";
import { useSeriesModification, useSeriesPagination } from "@/apis/hooks";
import { useArrInstanceLabels } from "@/apis/hooks/arrInstances";
import { useInstanceName } from "@/apis/hooks/site";
import { useUpgradableItems } from "@/apis/hooks/subtitles";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { Toolbox } from "@/components";
import { AudioList } from "@/components/bazarr";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ChangeProfileModal } from "@/components/forms/ChangeProfileForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MassCombineModal } from "@/components/forms/MassCombineForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import {
  MassTranslateModal,
  WantedItem,
} from "@/components/forms/MassTranslateForm";
import { SUBTITLE_TOOL_ACTIONS } from "@/constants/batch";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { GetItemId } from "@/utilities";

const SeriesView: FunctionComponent = () => {
  const mutation = useSeriesModification();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);
  const [instanceFilter, setInstanceFilter] = useState<string[]>([]);
  const {
    multiInstance,
    nameById: instanceNameById,
    defaultId: instanceDefaultId,
    options: instanceOptions,
  } = useArrInstanceLabels("sonarr");

  const query = useSeriesPagination(true);
  const { data: upgradableData } = useUpgradableItems();
  const upgradableSeriesIds = useMemo(
    () => new Set(upgradableData?.series ?? []),
    [upgradableData?.series],
  );

  const [selections, setSelections] = useState<Item.Series[]>([]);
  const [dirties, setDirties] = useState<Item.Series[]>([]);
  const setProfiles = useCallback(
    (id: number | null) => {
      const newItems = selections.map((v) => ({ ...v, profileId: id }));
      setDirties((dirty) => uniqBy([...newItems, ...dirty], GetItemId));
    },
    [selections],
  );

  const { mutateAsync } = mutation;

  const save = useCallback(() => {
    const chunkSize = 1000;
    const form: FormType.ModifyItem = { id: [], profileid: [] };
    dirties.forEach((v) => {
      const id = GetItemId(v);
      if (id) {
        form.id.push(id);
        form.profileid.push(v.profileId);
      }
    });
    const mutateInChunks = async (
      ids: number[],
      profileIds: (number | null)[],
    ) => {
      if (ids.length === 0) return;
      await mutateAsync({
        id: ids.slice(0, chunkSize),
        profileid: profileIds.slice(0, chunkSize),
      });
      await mutateInChunks(ids.slice(chunkSize), profileIds.slice(chunkSize));
    };
    return mutateInChunks(form.id, form.profileid);
  }, [dirties, mutateAsync]);

  const profileToolbar = useMemo(() => {
    if (selections.length === 0 && dirties.length === 0) return undefined;
    return (
      <Group gap="xs">
        <Toolbox.Button
          icon={faLanguage}
          disabled={selections.length === 0}
          onClick={() => {
            modals.openContextModal(ChangeProfileModal, {
              onSelect: setProfiles,
            });
          }}
        >
          Change Profile
        </Toolbox.Button>
        {dirties.length > 0 && (
          <>
            <Toolbox.Button icon={faUndo} onClick={() => setDirties([])}>
              Cancel
            </Toolbox.Button>
            <Toolbox.MutateButton
              icon={faCheck}
              promise={save}
              onSuccess={() => setDirties([])}
            >
              Save
            </Toolbox.MutateButton>
          </>
        )}
      </Group>
    );
  }, [selections, dirties, modals, setProfiles, save]);

  const columns = useMemo<ColumnDef<Item.Series>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => (
          <Checkbox
            aria-label="Select all"
            id="series-select-all"
            indeterminate={table.getIsSomeRowsSelected()}
            checked={table.getIsAllRowsSelected()}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            aria-label={`Select ${row.original.title}`}
            id={`series-select-${row.index}`}
            checked={row.getIsSelected()}
            onChange={row.getToggleSelectedHandler()}
          />
        ),
      },
      {
        id: "status",
        cell: ({ row: { original } }) => (
          <Group gap="xs" wrap="nowrap">
            <Tooltip
              label={
                original.monitored
                  ? "Monitored in Sonarr"
                  : "Unmonitored in Sonarr"
              }
            >
              <FontAwesomeIcon
                icon={original.monitored ? faBookmark : farBookmark}
              />
            </Tooltip>

            <Tooltip label={original.ended ? "Ended" : "Continuing"}>
              <FontAwesomeIcon icon={original.ended ? faStop : faPlay} />
            </Tooltip>
          </Group>
        ),
      },
      {
        id: "upgradable",
        cell: ({ row: { original } }) =>
          upgradableSeriesIds.has(original.sonarrSeriesId) ? (
            <Tooltip label="Low match score, upgrading may find a better subtitle">
              <FontAwesomeIcon
                icon={faCircleDown}
                color="var(--bz-text-tertiary)"
                size="sm"
              />
            </Tooltip>
          ) : null,
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original } }) => {
          const target = `/series/${original.id}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {original.title}
            </Anchor>
          );
        },
      },
      // Owning Sonarr instance (#156). Only shown when more than one Sonarr is
      // configured; the default instance is the common case so its rows render
      // a dim placeholder and only 2nd-instance items carry a visible badge.
      ...(multiInstance
        ? [
            {
              id: "instance",
              header: "Instance",
              cell: ({ row: { original } }) => {
                const instanceId = original.arr_instance_id;
                if (instanceId == null || instanceId === instanceDefaultId) {
                  return (
                    <Text size="sm" c="dimmed">
                      –
                    </Text>
                  );
                }
                return (
                  <Badge
                    size="sm"
                    variant="light"
                    color="grape"
                    leftSection={<FontAwesomeIcon icon={faServer} />}
                  >
                    {instanceNameById.get(instanceId) ?? `#${instanceId}`}
                  </Badge>
                );
              },
            } as ColumnDef<Item.Series>,
          ]
        : []),
      {
        header: "Audio",
        accessorKey: "audio_language",
        cell: ({
          row: {
            original: { audio_language: audioLanguage },
          },
        }) => {
          return <AudioList audios={audioLanguage}></AudioList>;
        },
      },
      {
        header: "Languages Profile",
        accessorKey: "profileId",
        cell: ({ row: { original } }) => {
          return (
            <LanguageProfileName
              index={original.profileId}
              empty=""
            ></LanguageProfileName>
          );
        },
      },
      {
        header: "Episodes",
        accessorKey: "episodeFileCount",
        cell: (row) => {
          const { episodeFileCount, episodeMissingCount, profileId, title } =
            row.row.original;

          const label = `${episodeFileCount - episodeMissingCount}/${episodeFileCount}`;
          return (
            <Progress.Root
              key={title}
              size="xl"
              radius="xl"
              style={{ minWidth: 80, background: "var(--bz-hover-bg)" }}
            >
              <Tooltip label={label} withArrow>
                <Progress.Section
                  value={
                    episodeFileCount === 0 || !profileId
                      ? 0
                      : (1.0 - episodeMissingCount / episodeFileCount) * 100.0
                  }
                  color={episodeMissingCount === 0 ? "brand" : "yellow"}
                  style={{ borderRadius: "var(--bz-radius-xl)" }}
                >
                  <Progress.Label
                    style={{ fontSize: "0.75rem", fontWeight: 600 }}
                  >
                    {label}
                  </Progress.Label>
                </Progress.Section>
              </Tooltip>
              {episodeMissingCount === episodeFileCount && (
                <Progress.Label
                  styles={{
                    label: {
                      position: "absolute",
                      top: "50%",
                      left: "50%",
                      transform: "translate(-50%, -50%)",
                      fontSize: "0.75rem",
                      fontWeight: 600,
                    },
                  }}
                >
                  {label}
                </Progress.Label>
              )}
            </Progress.Root>
          );
        },
      },
      {
        id: "actions",
        cell: ({ row: { original } }) => {
          const batchItem: BatchItem = {
            type: "series",
            sonarrSeriesId: original.sonarrSeriesId,
          };
          const wantedItem: WantedItem = {
            type: "series",
            sonarrSeriesId: original.sonarrSeriesId,
            title: original.title,
          };
          return (
            <Menu shadow="md" width={220} position="bottom-end">
              <Menu.Target>
                <Tooltip label="Actions">
                  <ActionIcon aria-label="Actions" variant="subtle" size="sm">
                    <FontAwesomeIcon icon={faEllipsisVertical} />
                  </ActionIcon>
                </Tooltip>
              </Menu.Target>
              <Menu.Dropdown>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faWrench} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(
                      ItemEditModal,
                      { mutation, item: original },
                      { title: original.title },
                    )
                  }
                >
                  Edit
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faSync} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(MassSyncModal, {
                      items: [batchItem],
                    })
                  }
                >
                  Sync Subtitles
                </Menu.Item>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faLanguage} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(MassTranslateModal, {
                      items: [wantedItem],
                    })
                  }
                >
                  Translate
                </Menu.Item>
                <Menu.Item
                  leftSection={
                    <FontAwesomeIcon icon={faLayerGroup} size="sm" />
                  }
                  onClick={() =>
                    modals.openContextModal(MassCombineModal, {
                      items: [wantedItem],
                    })
                  }
                >
                  Combine Subtitles
                </Menu.Item>
                <Menu.Divider />
                <Menu.Label>Subtitle Tools</Menu.Label>
                {SUBTITLE_TOOL_ACTIONS.map(([action, label]) => (
                  <Menu.Item
                    key={action}
                    onClick={() =>
                      modals.openContextModal(BatchModConfirmModal, {
                        items: [batchItem],
                        action,
                      })
                    }
                  >
                    {label}
                  </Menu.Item>
                ))}
                <Menu.Divider />
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(BatchModConfirmModal, {
                      items: [batchItem],
                      action: "scan-disk" as BatchAction,
                    })
                  }
                >
                  Scan Disk
                </Menu.Item>
                <Menu.Item
                  leftSection={
                    <FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />
                  }
                  onClick={() =>
                    modals.openContextModal(BatchModConfirmModal, {
                      items: [batchItem],
                      action: "search-missing" as BatchAction,
                    })
                  }
                >
                  Search Missing
                </Menu.Item>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faArrowUp} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(BatchModConfirmModal, {
                      items: [batchItem],
                      action: "upgrade" as BatchAction,
                    })
                  }
                >
                  Upgrade
                </Menu.Item>
              </Menu.Dropdown>
            </Menu>
          );
        },
      },
    ],
    [
      mutation,
      modals,
      upgradableSeriesIds,
      multiInstance,
      instanceNameById,
      instanceDefaultId,
    ],
  );

  const selectionToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;

    const toBatchItems = (): BatchItem[] =>
      selections.map((s) => ({
        type: "series" as const,
        sonarrSeriesId: s.sonarrSeriesId,
      }));

    const toWantedItems = (): WantedItem[] =>
      selections.map((s) => ({
        type: "series" as const,
        sonarrSeriesId: s.sonarrSeriesId,
        title: s.title,
      }));

    return (
      <Group gap="xs">
        <Toolbox.Button
          icon={faSync}
          onClick={() =>
            modals.openContextModal(MassSyncModal, { items: toBatchItems() })
          }
        >
          Sync Subtitles
        </Toolbox.Button>

        <Menu shadow="md" width={220}>
          <Menu.Target>
            <div>
              <Toolbox.Button icon={faToolbox}>Subtitle Tools</Toolbox.Button>
            </div>
          </Menu.Target>
          <Menu.Dropdown>
            {SUBTITLE_TOOL_ACTIONS.map(([action, label]) => (
              <Menu.Item
                key={action}
                onClick={() =>
                  modals.openContextModal(BatchModConfirmModal, {
                    items: toBatchItems(),
                    action,
                  })
                }
              >
                {label}
              </Menu.Item>
            ))}
          </Menu.Dropdown>
        </Menu>

        <Toolbox.Button
          icon={faLanguage}
          onClick={() =>
            modals.openContextModal(MassTranslateModal, {
              items: toWantedItems(),
            })
          }
        >
          Translate
        </Toolbox.Button>

        <Toolbox.Button
          icon={faLayerGroup}
          onClick={() =>
            modals.openContextModal(MassCombineModal, {
              items: toWantedItems(),
            })
          }
        >
          Combine
        </Toolbox.Button>

        <Menu shadow="md" width={220}>
          <Menu.Target>
            <div>
              <Toolbox.Button icon={faEllipsisVertical}>More</Toolbox.Button>
            </div>
          </Menu.Target>
          <Menu.Dropdown>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faHardDrive} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "scan-disk" as BatchAction,
                })
              }
            >
              Scan Disk
            </Menu.Item>
            <Menu.Item
              leftSection={
                <FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />
              }
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "search-missing" as BatchAction,
                })
              }
            >
              Search Missing
            </Menu.Item>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faArrowUp} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "upgrade" as BatchAction,
                })
              }
            >
              Upgrade
            </Menu.Item>
          </Menu.Dropdown>
        </Menu>
      </Group>
    );
  }, [selections, modals]);

  useDocumentTitle(`Series - ${useInstanceName()}`);

  return (
    <Container px={0} fluid>
      <ItemView
        query={query}
        columns={columns}
        searchValue={search}
        onSearchChange={setSearch}
        audioLanguages={audioLanguages}
        onAudioLanguagesChange={setAudioLanguages}
        excludeLanguages={excludeLanguages}
        onExcludeLanguagesChange={setExcludeLanguages}
        instanceOptions={multiInstance ? instanceOptions : undefined}
        instanceValues={instanceFilter}
        onInstanceValuesChange={setInstanceFilter}
        enableRowSelection
        onSelectionChanged={setSelections}
        selectionToolbar={selectionToolbar}
        profileToolbar={profileToolbar}
      ></ItemView>
    </Container>
  );
};

export default SeriesView;
