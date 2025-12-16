import { FunctionComponent, useCallback, useMemo } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Checkbox, Group } from "@mantine/core";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import {
  useEpisodeSubtitleModification,
  useEpisodeWantedPagination,
  useSeriesAction,
} from "@/apis/hooks";
import Language from "@/components/bazarr/Language";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { task, TaskGroup } from "@/modules/task";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";

const WantedSeriesView: FunctionComponent = () => {
  const { download } = useEpisodeSubtitleModification();

  const columns = useMemo<ColumnDef<Wanted.Episode>[]>(
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
            />
          );
        },
        cell: ({ row: { index, getIsSelected, getToggleSelectedHandler } }) => {
          return (
            <Checkbox
              id={`table-cell-${index}`}
              checked={getIsSelected()}
              onChange={getToggleSelectedHandler()}
              onClick={getToggleSelectedHandler()}
            />
          );
        },
      },
      {
        header: "Name",
        accessorKey: "seriesTitle",
        cell: ({
          row: {
            original: { sonarrSeriesId, seriesTitle },
          },
        }) => {
          const target = `/series/${sonarrSeriesId}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {seriesTitle}
            </Anchor>
          );
        },
      },
      {
        header: "Episode",
        accessorKey: "episode_number",
      },
      {
        accessorKey: "episodeTitle",
      },
      {
        header: "Missing",
        accessorKey: "missing_subtitles",
        cell: ({
          row: {
            original: {
              sonarrSeriesId,
              sonarrEpisodeId,
              missing_subtitles: missingSubtitles,
            },
          },
        }) => {
          const seriesId = sonarrSeriesId;
          const episodeId = sonarrEpisodeId;

          return (
            <Group gap="sm">
              {missingSubtitles.map((item, idx) => (
                <Badge
                  color={download.isPending ? "gray" : undefined}
                  leftSection={<FontAwesomeIcon icon={faSearch} />}
                  key={BuildKey(idx, item.code2)}
                  style={{ cursor: "pointer" }}
                  onClick={() => {
                    task.create(
                      item.name,
                      TaskGroup.SearchSubtitle,
                      download.mutateAsync,
                      {
                        seriesId,
                        episodeId,
                        form: {
                          language: item.code2,
                          hi: item.hi,
                          forced: item.forced,
                        },
                      },
                    );
                  }}
                >
                  <Language.Text value={item}></Language.Text>
                </Badge>
              ))}
            </Group>
          );
        },
      },
    ],
    [download],
  );

  const getWantedItem = useCallback((row: Wanted.Episode): WantedItem => {
    return {
      type: "episode",
      sonarrSeriesId: row.sonarrSeriesId,
      sonarrEpisodeId: row.sonarrEpisodeId,
      seriesTitle: row.seriesTitle,
      episodeTitle: row.episodeTitle,
    };
  }, []);

  const { mutateAsync } = useSeriesAction();
  const query = useEpisodeWantedPagination();
  return (
    <WantedView
      name="Series"
      columns={columns}
      query={query}
      searchAll={() => mutateAsync({ action: "search-wanted" })}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedSeriesView;
