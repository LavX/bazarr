import { FunctionComponent, useCallback, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Checkbox, Container, Group, Menu, Progress } from "@mantine/core";
import { useCombobox } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faCheck,
  faHardDrive,
  faLanguage,
  faMagnifyingGlass,
  faPlay,
  faStop,
  faSync,
  faToolbox,
  faUndo,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { uniqBy } from "lodash";
import { useLanguageProfiles, useSeriesModification, useSeriesPagination } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { Action, GroupedSelector, GroupedSelectorOptions, Toolbox } from "@/components";
import { AudioList } from "@/components/bazarr";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import { MassTranslateModal, WantedItem } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { GetItemId } from "@/utilities";
import { useSelectorOptions } from "@/utilities/hooks";

const SeriesView: FunctionComponent = () => {
  const mutation = useSeriesModification();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);

  const hasActiveFilter =
    search.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0;

  const query = useSeriesPagination(hasActiveFilter);

  const [selections, setSelections] = useState<Item.Series[]>([]);
  const [dirties, setDirties] = useState<Item.Series[]>([]);
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

  const columns = useMemo<ColumnDef<Item.Series>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => (
          <Checkbox
            id="series-select-all"
            indeterminate={table.getIsSomeRowsSelected()}
            checked={table.getIsAllRowsSelected()}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
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
            <FontAwesomeIcon
              title={original.monitored ? "monitored" : "unmonitored"}
              icon={original.monitored ? faBookmark : farBookmark}
            ></FontAwesomeIcon>

            <FontAwesomeIcon
              title={original.ended ? "Ended" : "Continuing"}
              icon={original.ended ? faStop : faPlay}
            ></FontAwesomeIcon>
          </Group>
        ),
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original } }) => {
          const target = `/series/${original.sonarrSeriesId}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {original.title}
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
            <Progress.Root key={title} size="xl">
              <Progress.Section
                value={
                  episodeFileCount === 0 || !profileId
                    ? 0
                    : (1.0 - episodeMissingCount / episodeFileCount) * 100.0
                }
                color={episodeMissingCount === 0 ? "brand" : "yellow"}
              >
                <Progress.Label>{label}</Progress.Label>
              </Progress.Section>
              {episodeMissingCount === episodeFileCount && (
                <Progress.Label
                  styles={{
                    label: {
                      position: "absolute",
                      top: "3px",
                      left: "50%",
                      transform: "translateX(-50%)",
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
        id: "sonarrSeriesId",
        cell: ({ row: { original } }) => {
          return (
            <Action
              label="Edit Series"
              tooltip={{ position: "left" }}
              onClick={() =>
                modals.openContextModal(
                  ItemEditModal,
                  {
                    mutation,
                    item: original,
                  },
                  {
                    title: original.title,
                  },
                )
              }
              icon={faWrench}
            ></Action>
          );
        },
      },
    ],
    [mutation, modals],
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
            {(
              [
                ["OCR_fixes", "OCR Fixes"],
                ["common", "Common Fixes"],
                ["remove_HI", "Remove Hearing Impaired"],
                ["remove_tags", "Remove Style Tags"],
                ["fix_uppercase", "Fix Uppercase"],
                ["reverse_rtl", "Reverse RTL"],
              ] as [BatchAction, string][]
            ).map(([action, label]) => (
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
        enableRowSelection
        onSelectionChanged={setSelections}
        selectionToolbar={selectionToolbar}
        profileToolbar={profileToolbar}
      ></ItemView>
    </Container>
  );
};

export default SeriesView;
