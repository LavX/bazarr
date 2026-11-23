import { FunctionComponent, useMemo } from "react";
import { Link } from "react-router";
import { Anchor, Text } from "@mantine/core";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { useMovieDeleteBlacklist } from "@/apis/hooks";
import type { useSourceFilter } from "@/apis/hooks/mediaSources";
import MutateAction from "@/components/async/MutateAction";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import PageTable from "@/components/tables/PageTable";
import TextPopover from "@/components/TextPopover";
import { blacklistRowId, blacklistRowRemoval } from "@/pages/Blacklist/row";
import SourceTitle from "@/pages/Blacklist/SourceTitle";

interface Props {
  blacklist: Blacklist.Movie[];
  filter: ReturnType<typeof useSourceFilter>;
}

const Table: FunctionComponent<Props> = ({ blacklist, filter }) => {
  const remove = useMovieDeleteBlacklist();
  const { showBadge, includeSources, labels, arrNames, defaultArr } = filter;
  const showSource =
    showBadge || blacklist.some((row) => row.origin === "source");

  const columns = useMemo<ColumnDef<Blacklist.Movie>[]>(
    () => [
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row: { original } }) => {
          const { id, title } = original;
          if (original.origin === "source") {
            return <SourceTitle title={title} row={original} />;
          }
          const target = `/movies/${id}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {title}
            </Anchor>
          );
        },
      },
      ...(showSource
        ? [
            {
              id: "source",
              header: includeSources ? "Source" : "Instance",
              cell: ({ row: { original } }) => (
                <SourceBadge
                  row={original}
                  labels={labels}
                  arrNames={arrNames}
                  defaultArr={defaultArr}
                />
              ),
            } satisfies ColumnDef<Blacklist.Movie>,
          ]
        : []),
      {
        header: "Language",
        accessorKey: "language",
        cell: ({
          row: {
            original: { language },
          },
        }) => {
          if (language) {
            return <Language.Text value={language} long></Language.Text>;
          } else {
            return null;
          }
        },
      },
      {
        header: "Provider",
        accessorKey: "provider",
      },
      {
        header: "Date",
        accessorKey: "timestamp",
        cell: ({
          row: {
            original: { timestamp, parsed_timestamp: parsedTimestamp },
          },
        }) => {
          if (timestamp) {
            return (
              <TextPopover text={parsedTimestamp}>
                <Text>{timestamp}</Text>
              </TextPopover>
            );
          } else {
            return null;
          }
        },
      },
      {
        id: "subs_id",
        cell: ({ row: { original } }) => {
          return (
            <MutateAction
              label="Remove from Excluded"
              icon={faTrash}
              mutation={remove}
              args={() => blacklistRowRemoval(original)}
            ></MutateAction>
          );
        },
      },
    ],
    [remove, showSource, includeSources, labels, arrNames, defaultArr],
  );
  return (
    <PageTable
      tableStyles={{ emptyText: "No blacklisted movies subtitles" }}
      columns={columns}
      data={blacklist}
      getRowId={blacklistRowId}
    ></PageTable>
  );
};

export default Table;
