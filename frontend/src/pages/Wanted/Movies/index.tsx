import { FunctionComponent, useCallback, useMemo } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Checkbox, Group } from "@mantine/core";
import { faSearch } from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import {
  useMovieAction,
  useMovieSubtitleModification,
  useMovieWantedPagination,
} from "@/apis/hooks";
import Language from "@/components/bazarr/Language";
import { WantedItem } from "@/components/forms/MassTranslateForm";
import { task, TaskGroup } from "@/modules/task";
import WantedView from "@/pages/views/WantedView";
import { BuildKey } from "@/utilities";

const WantedMoviesView: FunctionComponent = () => {
  const { download } = useMovieSubtitleModification();

  const columns = useMemo<ColumnDef<Wanted.Movie>[]>(
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
        accessorKey: "title",
        cell: ({
          row: {
            original: { title, radarrId },
          },
        }) => {
          const target = `/movies/${radarrId}`;
          return (
            <Anchor component={Link} to={target}>
              {title}
            </Anchor>
          );
        },
      },
      {
        header: "Missing",
        accessorKey: "missing_subtitles",
        cell: ({
          row: {
            original: { radarrId, missing_subtitles: missingSubtitles },
          },
        }) => {
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
                        radarrId,
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

  const getWantedItem = useCallback((row: Wanted.Movie): WantedItem => {
    return {
      type: "movie",
      radarrId: row.radarrId,
      title: row.title,
    };
  }, []);

  const { mutateAsync } = useMovieAction();
  const query = useMovieWantedPagination();

  return (
    <WantedView
      name="Movies"
      columns={columns}
      query={query}
      searchAll={() => mutateAsync({ action: "search-wanted" })}
      getWantedItem={getWantedItem}
    />
  );
};

export default WantedMoviesView;
