import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import { ActionIcon, Anchor, Badge, Checkbox, Container, Group, Menu, Tooltip } from "@mantine/core";
import { useCombobox } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faArrowUp,
  faBookmark,
  faCircleDown,
  faCheck,
  faEllipsisVertical,
  faHardDrive,
  faLanguage,
  faMagnifyingGlass,
  faSync,
  faToolbox,
  faUndo,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { uniqBy, uniqueId } from "lodash";
import { useLanguageProfiles, useMovieModification, useMoviesPagination } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { useUpgradableItems } from "@/apis/hooks/subtitles";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { SUBTITLE_TOOL_ACTIONS } from "@/constants/batch";
import { GroupedSelector, GroupedSelectorOptions, Toolbox } from "@/components";
import { AudioList } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import { MassTranslateModal, WantedItem } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { BuildKey, GetItemId } from "@/utilities";
import { useSelectorOptions } from "@/utilities/hooks";

const MovieView: FunctionComponent = () => {
  const modifyMovie = useMovieModification();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);

  const query = useMoviesPagination(true);
  const { data: upgradableData } = useUpgradableItems();
  const upgradableMovieIds = useMemo(
    () => new Set(upgradableData?.movies ?? []),
    [upgradableData?.movies],
  );

  const [selections, setSelections] = useState<Item.Movie[]>([]);
  const [dirties, setDirties] = useState<Item.Movie[]>([]);
  const { data: profiles } = useLanguageProfiles();
  const profileOptions = useSelectorOptions(profiles ?? [], (v) => v.name);
  const combobox = useCombobox();

  const profileOptionsWithAction = useMemo<GroupedSelectorOptions<string>[]>(() => [
    { group: "Actions", items: [{ label: "Clear", value: "", profileId: null }] },
    {
      group: "Profiles",
      items: profileOptions.options.map((a) => ({
        value: a.value.profileId.toString(),
        label: a.label,
        profileId: a.value.profileId,
      })),
    },
  ], [profileOptions.options]);

  const setProfiles = useCallback(
    (id: number | null) => {
      const newItems = selections.map((v) => ({ ...v, profileId: id }));
      setDirties((dirty) => uniqBy([...newItems, ...dirty], GetItemId));
    },
    [selections],
  );

  const { mutateAsync } = modifyMovie;

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
      await mutateAsync({ id: ids.slice(0, chunkSize), profileid: profileIds.slice(0, chunkSize) });
      await mutateInChunks(ids.slice(chunkSize), profileIds.slice(chunkSize));
    };
    return mutateInChunks(form.id, form.profileid);
  }, [dirties, mutateAsync]);

  const profileToolbar = useMemo(() => {
    if (selections.length === 0 && dirties.length === 0) return undefined;
    return (
      <Group gap="xs">
        <GroupedSelector
          onClick={() => combobox.openDropdown()}
          onDropdownClose={() => combobox.resetSelectedOption()}
          placeholder="Change Profile"
          withCheckIcon={false}
          options={profileOptionsWithAction}
          disabled={selections.length === 0}
          comboboxProps={{
            store: combobox,
            onOptionSubmit: (value) => setProfiles(value ? +value : null),
          }}
        />
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
  }, [selections, dirties, combobox, profileOptionsWithAction, setProfiles, save]);

  const columns = useMemo<ColumnDef<Item.Movie>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => (
          <Checkbox
            aria-label="Select all"
            id="movies-select-all"
            indeterminate={table.getIsSomeRowsSelected()}
            checked={table.getIsAllRowsSelected()}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
            aria-label={`Select ${row.original.title}`}
            id={`movies-select-${row.index}`}
            checked={row.getIsSelected()}
            onChange={row.getToggleSelectedHandler()}
          />
        ),
      },
      {
        id: "monitored",
        cell: ({
          row: {
            original: { monitored },
          },
        }) => (
          <Tooltip
            label={monitored ? "Monitored in Radarr" : "Unmonitored in Radarr"}
          >
            <FontAwesomeIcon icon={monitored ? faBookmark : farBookmark} />
          </Tooltip>
        ),
      },
      {
        id: "upgradable",
        cell: ({
          row: {
            original: { radarrId },
          },
        }) =>
          upgradableMovieIds.has(radarrId) ? (
            <Tooltip label="Low match score, upgrading may find a better subtitle">
              <FontAwesomeIcon icon={faCircleDown} color="var(--bz-text-tertiary)" size="sm" />
            </Tooltip>
          ) : null,
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({
          row: {
            original: { title, radarrId },
          },
        }) => {
          const target = `/movies/${radarrId}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {title}
            </Anchor>
          );
        },
      },
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
        cell: ({
          row: {
            original: { profileId },
          },
        }) => {
          return (
            <LanguageProfileName
              index={profileId}
              empty=""
            ></LanguageProfileName>
          );
        },
      },
      {
        header: "Missing Subtitles",
        accessorKey: "missing_subtitles",
        cell: ({
          row: {
            original: { missing_subtitles: missingSubtitles },
          },
        }) => {
          return (
            <>
              {missingSubtitles.map((v) => (
                <Badge
                  mr="xs"
                  color="yellow"
                  key={uniqueId(`${BuildKey(v.code2, v.hi, v.forced)}_`)}
                >
                  <Language.Text value={v}></Language.Text>
                </Badge>
              ))}
            </>
          );
        },
      },
      {
        id: "actions",
        cell: ({ row }) => {
          const item = row.original;
          const batchItem: BatchItem = { type: "movie", radarrId: item.radarrId };
          const wantedItem: WantedItem = { type: "movie", radarrId: item.radarrId, title: item.title };
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
                      { mutation: modifyMovie, item },
                      { title: item.title },
                    )
                  }
                >
                  Edit
                </Menu.Item>
                <Menu.Divider />
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faSync} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(MassSyncModal, { items: [batchItem] })
                  }
                >
                  Sync Subtitles
                </Menu.Item>
                <Menu.Item
                  leftSection={<FontAwesomeIcon icon={faLanguage} size="sm" />}
                  onClick={() =>
                    modals.openContextModal(MassTranslateModal, { items: [wantedItem] })
                  }
                >
                  Translate
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
                  leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} size="sm" />}
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
    [modals, modifyMovie, upgradableMovieIds],
  );

  const selectionToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;

    const toBatchItems = (): BatchItem[] =>
      selections.map((m) => ({
        type: "movie" as const,
        radarrId: m.radarrId,
      }));

    const toWantedItems = (): WantedItem[] =>
      selections.map((m) => ({
        type: "movie" as const,
        radarrId: m.radarrId,
        title: m.title,
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
          icon={faHardDrive}
          onClick={() =>
            modals.openContextModal(BatchModConfirmModal, {
              items: toBatchItems(),
              action: "scan-disk" as BatchAction,
            })
          }
        >
          Scan Disk
        </Toolbox.Button>

        <Toolbox.Button
          icon={faMagnifyingGlass}
          onClick={() =>
            modals.openContextModal(BatchModConfirmModal, {
              items: toBatchItems(),
              action: "search-missing" as BatchAction,
            })
          }
        >
          Search Missing
        </Toolbox.Button>

        <Toolbox.Button
          icon={faArrowUp}
          onClick={() =>
            modals.openContextModal(BatchModConfirmModal, {
              items: toBatchItems(),
              action: "upgrade" as BatchAction,
            })
          }
        >
          Upgrade
        </Toolbox.Button>
      </Group>
    );
  }, [selections, modals]);

  useDocumentTitle(`Movies - ${useInstanceName()}`);

  return (
    <Container fluid px={0}>
      <ItemView
        query={query}
        columns={columns}
        searchValue={search}
        onSearchChange={setSearch}
        audioLanguages={audioLanguages}
        onAudioLanguagesChange={setAudioLanguages}
        excludeLanguages={excludeLanguages}
        onExcludeLanguagesChange={setExcludeLanguages}
        enableRowSelection
        onSelectionChanged={setSelections}
        selectionToolbar={selectionToolbar}
        profileToolbar={profileToolbar}
      ></ItemView>
    </Container>
  );
};

export default MovieView;
