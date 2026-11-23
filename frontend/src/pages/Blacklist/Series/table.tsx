import { FunctionComponent, useMemo } from "react";
import { Link } from "react-router";
import { Anchor, Text } from "@mantine/core";
import { faTrash } from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { useEpisodeDeleteBlacklist } from "@/apis/hooks";
import type { useSourceFilter } from "@/apis/hooks/mediaSources";
import MutateAction from "@/components/async/MutateAction";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import PageTable from "@/components/tables/PageTable";
import TextPopover from "@/components/TextPopover";
import { blacklistRowId, blacklistRowRemoval } from "@/pages/Blacklist/row";
import SourceTitle from "@/pages/Blacklist/SourceTitle";

interface Props {
  blacklist: Blacklist.Episode[];
  filter: ReturnType<typeof useSourceFilter>;
}

const Table: FunctionComponent<Props> = ({ blacklist, filter }) => {
  const removeFromBlacklist = useEpisodeDeleteBlacklist();
  const { showBadge, includeSources, labels, arrNames, defaultArr } = filter;
  const showSource =
    showBadge || blacklist.some((row) => row.origin === "source");

  const columns = useMemo<ColumnDef<Blacklist.Episode>[]>(
    () => [
      {
        header: "Series",
        accessorKey: "seriesTitle",
        cell: ({ row: { original } }) => {
          const { id, seriesTitle } = original;
          if (original.origin === "source") return <Text>{seriesTitle}</Text>;
          const target = `/series/${id}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {seriesTitle}
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
            } satisfies ColumnDef<Blacklist.Episode>,
          ]
        : []),
      {
        header: "Episode",
        accessorKey: "episode_number",
      },
      {
        header: "Title",
        accessorKey: "episodeTitle",
        cell: ({ row: { original } }) => {
          if (original.origin === "source") {
            return <SourceTitle title={original.episodeTitle} row={original} />;
          }
          return <Text className="table-no-wrap">{original.episodeTitle}</Text>;
        },
      },
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
              mutation={removeFromBlacklist}
              args={() => blacklistRowRemoval(original)}
            ></MutateAction>
          );
        },
      },
    ],
    [
      removeFromBlacklist,
      showSource,
      includeSources,
      labels,
      arrNames,
      defaultArr,
    ],
  );
  return (
    <PageTable
      tableStyles={{ emptyText: "No blacklisted series subtitles" }}
      columns={columns}
      data={blacklist}
      getRowId={blacklistRowId}
    ></PageTable>
  );
};

export default Table;
