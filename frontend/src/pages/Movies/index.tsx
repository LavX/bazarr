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
  Tooltip,
} from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faArrowUp,
  faBookmark,
  faCircleDown,
  faEllipsisVertical,
  faFileExport,
  faHardDrive,
  faLanguage,
  faLayerGroup,
  faMagnifyingGlass,
  faSpellCheck,
  faSync,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { useMovieModification, useSystemSettings } from "@/apis/hooks";
import { useMovieLibrary } from "@/apis/hooks/library";
import { useAppTitle } from "@/apis/hooks/site";
import { useUpgradableItems } from "@/apis/hooks/subtitles";
import {
  getLibraryOwnerKey,
  getLibraryRowKey,
  libraryItemPath,
  LibraryRow,
} from "@/apis/raw/library";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { Toolbox } from "@/components";
import { AudioList, ScorePill } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import SourceBadge from "@/components/bazarr/SourceBadge";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MassCombineModal } from "@/components/forms/MassCombineForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import {
  MassTranslateModal,
  WantedItem,
} from "@/components/forms/MassTranslateForm";
import { SUBTITLE_TOOL_ACTIONS } from "@/constants/batch";
import { useModals } from "@/modules/modals";
import ItemView, {
  ScoreFilter,
  SubtitlesFilter,
  SubtitlesStatus,
} from "@/pages/views/ItemView";
import {
  LibrarySourceRowActions,
  LibrarySourceSelectionActions,
} from "@/pages/views/LibrarySourceActions";
import { useLibraryProfileEdit } from "@/pages/views/useLibraryProfileEdit";
import { BuildKey } from "@/utilities";

function upgradableKey(upstreamId: number, arrInstanceId?: number | null) {
  return `${upstreamId}:${arrInstanceId ?? ""}`;
}

const MovieView: FunctionComponent = () => {
  const modifyMovie = useMovieModification();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);
  const [subtitlesFilter, setSubtitlesFilter] =
    useState<SubtitlesFilter>("any");
  const [scoreFilter, setScoreFilter] = useState<ScoreFilter>("any");
  const [tagFilter, setTagFilter] = useState<string[]>([]);

  // The acceptance threshold that labels the "below threshold" score group and
  // drives it; undefined until settings load. Movies use the movie minimum.
  const { data: settings } = useSystemSettings();
  const scoreThreshold = settings?.general.minimum_score_movie;

  const scoreValue = useCallback(
    (movie: LibraryRow<Item.Movie>) => movie.lowest_subtitle_score ?? null,
    [],
  );

  // A movie with no language profile has nothing to complete, so it is
  // untracked and belongs to neither group. Otherwise it is complete when
  // nothing is missing.
  const subtitlesStatus = useCallback(
    (movie: LibraryRow<Item.Movie>): SubtitlesStatus => {
      if (movie.profileId == null) return "untracked";
      return !movie.missing_subtitles?.length ? "complete" : "missing";
    },
    [],
  );

  // Ask the backend for the lowest-score aggregate only while the score filter
  // is active, mirroring how the list already fetches all rows for filtering.
  const {
    query,
    labels,
    arrNames,
    defaultArr,
    hasSources,
    showBadge,
    showAttention,
    ownerFilter,
    canUseSourceLibrary,
    needsAttention,
    onNeedsAttentionChange,
    clearSourceFilters,
  } = useMovieLibrary(scoreFilter !== "any");
  const { data: upgradableData } = useUpgradableItems();
  const upgradableMovieKeys = useMemo(
    () =>
      new Set(
        upgradableData?.movieKeys?.map((item) =>
          upgradableKey(item.radarrId, item.arr_instance_id),
        ) ??
          upgradableData?.movies.map((id) => upgradableKey(id)) ??
          [],
      ),
    [upgradableData?.movieKeys, upgradableData?.movies],
  );

  const [selections, setSelections] = useState<LibraryRow<Item.Movie>[]>([]);
  const { profileToolbar } = useLibraryProfileEdit(
    "movies",
    selections,
    modifyMovie.mutateAsync,
  );

  const columns = useMemo<ColumnDef<LibraryRow<Item.Movie>>[]>(
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
        cell: ({ row: { original } }) =>
          original.origin === "source" ? null : (
            <Tooltip
              label={
                original.monitored
                  ? "Monitored in Radarr"
                  : "Unmonitored in Radarr"
              }
            >
              <FontAwesomeIcon
                icon={original.monitored ? faBookmark : farBookmark}
              />
            </Tooltip>
          ),
      },
      {
        id: "upgradable",
        cell: ({ row: { original } }) =>
          original.origin !== "source" &&
          upgradableMovieKeys.has(
            upgradableKey(original.radarrId, original.arr_instance_id),
          ) ? (
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
          const target = libraryItemPath("movies", original);
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {original.title}
            </Anchor>
          );
        },
      },
      ...(showBadge
        ? [
            {
              id: "instance",
              header: hasSources ? "Source" : "Instance",
              cell: ({ row: { original } }) => (
                <SourceBadge
                  row={original}
                  labels={labels}
                  arrNames={arrNames}
                  defaultArr={defaultArr}
                />
              ),
            } as ColumnDef<LibraryRow<Item.Movie>>,
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
              {missingSubtitles?.map((v) => (
                <Badge
                  mr="xs"
                  color="yellow"
                  key={BuildKey(v.code2, v.hi, v.forced)}
                >
                  <Language.Text value={v}></Language.Text>
                </Badge>
              ))}
            </>
          );
        },
      },
      // Lowest current-subtitle score, shown only while the score filter is
      // narrowing the list. A dim dash marks a movie with no known score.
      ...(scoreFilter !== "any"
        ? [
            {
              id: "lowestScore",
              header: "Lowest score",
              cell: ({
                row: {
                  original: { lowest_subtitle_score: lowestScore },
                },
              }) => <ScorePill score={lowestScore} />,
            } as ColumnDef<LibraryRow<Item.Movie>>,
          ]
        : []),
      {
        id: "actions",
        cell: ({ row }) => {
          const item = row.original;
          if (item.origin === "source")
            return (
              <LibrarySourceRowActions
                kind="movies"
                row={item}
                canUseLibrary={canUseSourceLibrary}
              />
            );
          const batchItem: BatchItem = {
            type: "movie",
            radarrId: item.radarrId,
            arr_instance_id: item.arr_instance_id ?? undefined,
          };
          const wantedItem: WantedItem = {
            type: "movie",
            radarrId: item.radarrId,
            title: item.title,
            arrInstanceId: item.arr_instance_id ?? undefined,
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
                    <FontAwesomeIcon icon={faFileExport} size="sm" />
                  }
                  onClick={() =>
                    modals.openContextModal(BatchModConfirmModal, {
                      items: [batchItem],
                      action: "extract" as BatchAction,
                    })
                  }
                >
                  Extract Embedded Tracks
                </Menu.Item>
                <Menu.Item
                  leftSection={
                    <FontAwesomeIcon icon={faSpellCheck} size="sm" />
                  }
                  onClick={() =>
                    modals.openContextModal(BatchModConfirmModal, {
                      items: [batchItem],
                      action: "ocr" as BatchAction,
                    })
                  }
                >
                  OCR Image Subtitles
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
      modals,
      modifyMovie,
      upgradableMovieKeys,
      showBadge,
      canUseSourceLibrary,
      hasSources,
      labels,
      arrNames,
      defaultArr,
      scoreFilter,
    ],
  );

  const selectionToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;
    const sourceRows = selections.filter((item) => item.origin === "source");
    const arrRows = selections.filter((item) => item.origin !== "source");

    const toBatchItems = (): BatchItem[] =>
      arrRows.map((m) => ({
        type: "movie" as const,
        radarrId: m.radarrId,
        arr_instance_id: m.arr_instance_id ?? undefined,
      }));

    if (sourceRows.length > 0)
      return (
        <LibrarySourceSelectionActions
          kind="movies"
          rows={sourceRows}
          arrItems={toBatchItems()}
          canUseLibrary={canUseSourceLibrary}
        />
      );

    const toWantedItems = (): WantedItem[] =>
      arrRows.map((m) => ({
        type: "movie" as const,
        radarrId: m.radarrId,
        title: m.title,
        arrInstanceId: m.arr_instance_id ?? undefined,
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
              leftSection={<FontAwesomeIcon icon={faFileExport} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "extract" as BatchAction,
                })
              }
            >
              Extract Embedded Tracks
            </Menu.Item>
            <Menu.Item
              leftSection={<FontAwesomeIcon icon={faSpellCheck} size="sm" />}
              onClick={() =>
                modals.openContextModal(BatchModConfirmModal, {
                  items: toBatchItems(),
                  action: "ocr" as BatchAction,
                })
              }
            >
              OCR Image Subtitles
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
  }, [selections, modals, canUseSourceLibrary]);

  useDocumentTitle(`Movies - ${useAppTitle()}`);

  return (
    <Container fluid px={0}>
      <ItemView
        query={query}
        columns={columns}
        searchValue={search}
        onSearchChange={setSearch}
        audioLanguages={audioLanguages}
        includeSourceAudio={hasSources}
        onAudioLanguagesChange={setAudioLanguages}
        excludeLanguages={excludeLanguages}
        onExcludeLanguagesChange={setExcludeLanguages}
        subtitlesFilter={subtitlesFilter}
        onSubtitlesFilterChange={setSubtitlesFilter}
        subtitlesStatus={subtitlesStatus}
        scoreFilter={scoreFilter}
        onScoreFilterChange={setScoreFilter}
        scoreValue={scoreValue}
        scoreThreshold={scoreThreshold}
        instanceOptions={ownerFilter.options}
        instanceValues={ownerFilter.values}
        onInstanceValuesChange={ownerFilter.onChange}
        instanceLabel={ownerFilter.label}
        showSingleInstance={ownerFilter.showSingle}
        getInstanceValue={getLibraryOwnerKey}
        needsAttention={needsAttention}
        onNeedsAttentionChange={
          showAttention ? onNeedsAttentionChange : undefined
        }
        clearSourceFilters={clearSourceFilters}
        getRowId={getLibraryRowKey}
        tagValues={tagFilter}
        onTagValuesChange={setTagFilter}
        enableRowSelection
        onSelectionChanged={setSelections}
        selectionToolbar={selectionToolbar}
        profileToolbar={profileToolbar}
        itemNoun={{ one: "movie", other: "movies" }}
      ></ItemView>
    </Container>
  );
};

export default MovieView;
