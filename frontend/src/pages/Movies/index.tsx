import { FunctionComponent, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Checkbox, Container, Group, Menu, Tooltip } from "@mantine/core";
import { useDocumentTitle } from "@mantine/hooks";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faHardDrive,
  faLanguage,
  faMagnifyingGlass,
  faSync,
  faToolbox,
  faWrench,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { uniqueId } from "lodash";
import { useMovieModification, useMoviesPagination } from "@/apis/hooks";
import { useInstanceName } from "@/apis/hooks/site";
import { Action, Toolbox } from "@/components";
import { AudioList } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import LanguageProfileName from "@/components/bazarr/LanguageProfile";
import { BatchModConfirmModal } from "@/components/forms/BatchModConfirmForm";
import { ItemEditModal } from "@/components/forms/ItemEditForm";
import { MassSyncModal } from "@/components/forms/MassSyncForm";
import { MassTranslateModal } from "@/components/forms/MassTranslateForm";
import { useModals } from "@/modules/modals";
import ItemView from "@/pages/views/ItemView";
import { BatchAction, BatchItem } from "@/apis/raw/subtitles";
import { BuildKey } from "@/utilities";

const MovieView: FunctionComponent = () => {
  const modifyMovie = useMovieModification();
  const modals = useModals();

  const [search, setSearch] = useState("");
  const [audioLanguages, setAudioLanguages] = useState<string[]>([]);
  const [excludeLanguages, setExcludeLanguages] = useState<string[]>([]);

  const hasActiveFilter =
    search.length > 0 ||
    audioLanguages.length > 0 ||
    excludeLanguages.length > 0;

  const query = useMoviesPagination(hasActiveFilter);

  const [selections, setSelections] = useState<Item.Movie[]>([]);

  const columns = useMemo<ColumnDef<Item.Movie>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => (
          <Checkbox
            id="movies-select-all"
            indeterminate={table.getIsSomeRowsSelected()}
            checked={table.getIsAllRowsSelected()}
            onChange={table.getToggleAllRowsSelectedHandler()}
          />
        ),
        cell: ({ row }) => (
          <Checkbox
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
        id: "radarrId",
        cell: ({ row }) => {
          return (
            <Action
              label="Edit Movie"
              tooltip={{ position: "left" }}
              onClick={() =>
                modals.openContextModal(
                  ItemEditModal,
                  {
                    mutation: modifyMovie,
                    item: row.original,
                  },
                  {
                    title: row.original.title,
                  },
                )
              }
              icon={faWrench}
            ></Action>
          );
        },
      },
    ],
    [modals, modifyMovie],
  );

  const selectionToolbar = useMemo(() => {
    if (selections.length === 0) return undefined;

    const toBatchItems = (): BatchItem[] =>
      selections.map((m) => ({
        type: "movie" as const,
        radarrId: m.radarrId,
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
              items: toBatchItems(),
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
      ></ItemView>
    </Container>
  );
};

export default MovieView;
