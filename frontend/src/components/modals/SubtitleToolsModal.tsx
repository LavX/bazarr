import { FunctionComponent, useMemo, useState } from "react";
import {
  Badge,
  Button,
  Checkbox,
  Divider,
  Group,
  Stack,
  Text,
} from "@mantine/core";
import { ColumnDef } from "@tanstack/react-table";
import {
  useEpisodeSubtitleModification,
  useMovieSubtitleModification,
  useSubtitleFileDownload,
} from "@/apis/hooks";
import { useSportsSubtitleModification } from "@/apis/hooks/sports";
import Language from "@/components/bazarr/Language";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import SimpleTable from "@/components/tables/SimpleTable";
import { useModals, withModal } from "@/modules/modals";
import { fromPython, isMovie, toPython } from "@/utilities";
import {
  buildSubtitleLanguageKey,
  getCombinedLabel,
  getSyncEngineLabel,
  isCombinedOutputSubtitle,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";

// A sports event in the shape this modal reads. Its subtitles arrive as
// [language, path, size] tuples rather than Subtitle objects, so the caller
// maps them; everything else the modal needs is already on the event.
export type SportsToolsItem = {
  id: number;
  title: string;
  arr_instance_id: number;
  subtitles: Subtitle[];
  isSports: true;
};

type SupportType = Item.Episode | Item.Movie | SportsToolsItem;

type TableColumnType = FormType.ModifySubtitle & {
  raw_language: Subtitle;
  seriesId: number;
  name: string;
  isMovie: boolean;
};

type LocalisedType = {
  id: number;
  seriesId: number;
  type: "movie" | "episode" | "sports";
  name: string;
  isMovie: boolean;
  arr_instance_id?: number;
};

function isSports(item: SupportType): item is SportsToolsItem {
  return (item as SportsToolsItem).isSports === true;
}

function getLocalisedValues(item: SupportType): LocalisedType {
  if (isSports(item)) {
    return {
      seriesId: 0,
      // The local event id, which is what every sports route is keyed on.
      id: item.id,
      type: "sports",
      name: item.title,
      isMovie: false,
      // eslint-disable-next-line camelcase
      arr_instance_id: item.arr_instance_id,
    };
  }
  if (isMovie(item)) {
    return {
      seriesId: 0,
      id: item.radarrId,
      type: "movie",
      name: item.title,
      isMovie: true,
      // eslint-disable-next-line camelcase
      arr_instance_id: item.arr_instance_id,
    };
  } else {
    return {
      seriesId: item.sonarrSeriesId,
      id: item.sonarrEpisodeId,
      type: "episode",
      name: item.title,
      isMovie: false,
      // eslint-disable-next-line camelcase
      arr_instance_id: item.arr_instance_id,
    };
  }
}

interface SubtitleToolViewProps {
  payload: SupportType[];
}

const SubtitleToolView: FunctionComponent<SubtitleToolViewProps> = ({
  payload,
}) => {
  const [selections, setSelections] = useState<TableColumnType[]>([]);
  const { remove: removeEpisode, download: downloadEpisode } =
    useEpisodeSubtitleModification();
  const { download: downloadMovie, remove: removeMovie } =
    useMovieSubtitleModification();
  const { download: downloadSports, remove: removeSports } =
    useSportsSubtitleModification();
  const fileDownload = useSubtitleFileDownload();
  const modals = useModals();

  const columns = useMemo<ColumnDef<TableColumnType>[]>(
    () => [
      {
        id: "selection",
        header: ({ table }) => {
          return (
            <Checkbox
              id="table-header-selection"
              indeterminate={table.getIsSomeRowsSelected()}
              checked={table.getIsAllRowsSelected()}
              onChange={table.getToggleAllRowsSelectedHandler()}
            ></Checkbox>
          );
        },
        cell: ({ row: { index, getIsSelected, getToggleSelectedHandler } }) => {
          return (
            <Checkbox
              id={`table-cell-${index}`}
              checked={getIsSelected()}
              onChange={getToggleSelectedHandler()}
              onClick={getToggleSelectedHandler()}
            ></Checkbox>
          );
        },
      },
      {
        header: "Language",
        accessorKey: "raw_language",
        cell: ({
          row: {
            original: { raw_language: rawLanguage },
          },
        }) => (
          // The language name alone cannot tell two rows of the same language
          // apart: a sync or combined output carries the base language and
          // flags of the subtitle it was made from, so the variant rides along
          // in a second badge, the way the episode and movie tables show it.
          <Group gap={4} wrap="nowrap">
            <Badge color="secondary">
              <Language.Text value={rawLanguage} long></Language.Text>
            </Badge>
            {isSyncOutputSubtitle(rawLanguage) && (
              <Badge
                color="gray"
                size="xs"
                variant="light"
                style={{ whiteSpace: "nowrap" }}
              >
                {getSyncEngineLabel(rawLanguage.modifier)}
              </Badge>
            )}
            {isCombinedOutputSubtitle(rawLanguage) && (
              <Badge
                color="gray"
                size="xs"
                variant="light"
                style={{ whiteSpace: "nowrap" }}
              >
                {getCombinedLabel(rawLanguage)}
              </Badge>
            )}
          </Group>
        ),
      },
      {
        id: "file",
        header: "File",
        accessorKey: "path",
        cell: ({
          row: {
            original: { path },
          },
        }) => {
          let idx = path.lastIndexOf("/");

          if (idx === -1) {
            idx = path.lastIndexOf("\\");
          }

          if (idx !== -1) {
            return <Text>{path.slice(idx + 1)}</Text>;
          } else {
            return <Text>{path}</Text>;
          }
        },
      },
    ],
    [],
  );

  const data = useMemo<TableColumnType[]>(
    () =>
      payload.flatMap((item) => {
        const { seriesId, id, type, name, isMovie, arr_instance_id } =
          getLocalisedValues(item);
        return item.subtitles.flatMap((v) => {
          if (v.path) {
            return [
              {
                id,
                seriesId,
                type,
                language: v.code2,
                path: v.path,
                // eslint-disable-next-line camelcase
                raw_language: v,
                name,
                hi: toPython(v.hi),
                forced: toPython(v.forced),
                isMovie,
                // eslint-disable-next-line camelcase
                arr_instance_id,
              },
            ];
          } else {
            return [];
          }
        });
      }),
    [payload],
  );

  return (
    <Stack>
      <SimpleTable
        tableStyles={{ emptyText: "No external subtitles found" }}
        enableRowSelection={true}
        onRowSelectionChanged={(rows) =>
          setSelections(rows.map((r) => r.original))
        }
        columns={columns}
        data={data}
      ></SimpleTable>
      <Divider></Divider>
      <Group>
        <SubtitleToolsMenu
          selections={selections}
          onAction={async (action) => {
            if (action === "download") {
              // Sequential on purpose: parallel programmatic anchor clicks
              // are dropped by browsers that restrict multiple automatic
              // downloads; one at a time surfaces the browser's own
              // multi-download permission prompt instead.
              for (const selection of selections) {
                try {
                  await fileDownload.mutateAsync({
                    type: selection.type,
                    mediaId: selection.id,
                    language: buildSubtitleLanguageKey(selection.raw_language),
                    arrInstanceId: selection.arr_instance_id,
                  });
                } catch {
                  // The hook already notified; continue with the rest.
                }
              }
              modals.closeAll();
              return;
            }
            selections.forEach(async (selection) => {
              if (selection.type === "sports") {
                // Sports routes take the local event id and its owner, not the
                // series/movie id pair the other two build below.
                const owner = selection.arr_instance_id;
                if (owner === undefined) return;
                if (action === "search") {
                  await downloadSports.mutateAsync({
                    eventId: selection.id,
                    owner,
                  });
                } else if (action === "delete" && selection.path) {
                  await removeSports.mutateAsync({
                    eventId: selection.id,
                    owner,
                    form: {
                      language: selection.language,
                      path: selection.path,
                      hi: fromPython(selection.hi),
                      forced: fromPython(selection.forced),
                    },
                  });
                }
                return;
              }
              const actionPayload = {
                form: {
                  language: selection.language,
                  hi: fromPython(selection.hi),
                  forced: fromPython(selection.forced),
                  path: selection.path,
                },
                radarrId: 0,
                seriesId: 0,
                episodeId: 0,
                arrInstanceId: selection.arr_instance_id,
              };
              if (selection.isMovie) {
                actionPayload.radarrId = selection.id;
              } else {
                actionPayload.seriesId = selection.seriesId;
                actionPayload.episodeId = selection.id;
              }
              const download = selection.isMovie
                ? downloadMovie
                : downloadEpisode;
              const remove = selection.isMovie ? removeMovie : removeEpisode;

              if (action === "search") {
                await download.mutateAsync(actionPayload);
              } else if (action === "delete" && selection.path) {
                await remove.mutateAsync(actionPayload);
              }
            });
            modals.closeAll();
          }}
        >
          <Button disabled={selections.length === 0} variant="light">
            Select Action
          </Button>
        </SubtitleToolsMenu>
      </Group>
    </Stack>
  );
};

export default withModal(SubtitleToolView, "subtitle-tools", {
  title: "Subtitle Tools",
  size: "xl",
});
