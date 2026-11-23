/* eslint-disable camelcase */
import { FunctionComponent, useMemo, useState } from "react";
import { Link } from "react-router";
import { Anchor, Badge, Group, Switch, Text } from "@mantine/core";
import { faFileExcel } from "@fortawesome/free-solid-svg-icons";
import { CellContext, ColumnDef } from "@tanstack/react-table";
import { useMovieAddBlacklist, useMovieHistoryPagination } from "@/apis/hooks";
import { MutateAction } from "@/components/async";
import HistoryProvider from "@/components/bazarr/HistoryProvider";
import Language from "@/components/bazarr/Language";
import SourceBadge from "@/components/bazarr/SourceBadge";
import PlayOnMenu from "@/components/PlayOnMenu";
import StateIcon from "@/components/StateIcon";
import TextPopover from "@/components/TextPopover";
import {
  HistoryAction,
  historyAnchors,
  HistoryInfo,
  HistoryUpgrade,
} from "@/pages/History/HistoryCells";
import {
  useHistorySourcePresentation,
  useHistorySources,
} from "@/pages/History/sourceHistory";
import HistoryView from "@/pages/views/HistoryView";
import SourceFilter from "@/pages/views/SourceFilter";

// Defined once, so the columns being rebuilt never remount an open menu.
const MoviePlayCell = ({ row }: CellContext<History.MovieRow, unknown>) =>
  row.original.origin === "source" ? null : (
    <PlayOnMenu
      mediaType="movie"
      id={row.original.id}
      itemLabel={row.original.title}
      variant="action"
    />
  );

const MoviesHistoryView: FunctionComponent = () => {
  const addToBlacklist = useMovieAddBlacklist();
  const [includeEmbedded, setIncludeEmbedded] = useState(false);
  const sources = useHistorySources("radarr");
  const query = useMovieHistoryPagination(
    includeEmbedded,
    sources.options,
    sources.enabled,
  );
  const { labels, nativeFallback, selected } = useHistorySourcePresentation(
    sources,
    query,
  );
  const { names: arrNames, defaultId: defaultArr } = sources.arrOwners;
  const showBadge = !nativeFallback;
  const rows = query.data?.data;
  const anchors = useMemo(() => historyAnchors(rows ?? []), [rows]);

  const columns = useMemo<ColumnDef<History.MovieRow>[]>(
    () => [
      {
        id: "action",
        cell: ({ row: { original } }) => <HistoryAction row={original} />,
      },
      {
        header: "Name",
        accessorKey: "title",
        cell: ({ row }) => {
          if (row.original.origin === "source") {
            return row.original.item_title || row.original.video_path;
          }
          const target = `/movies/${row.original.id}`;
          return (
            <Anchor className="table-primary" component={Link} to={target}>
              {row.original.title}
            </Anchor>
          );
        },
      },
      { id: "play", cell: MoviePlayCell },
      ...(showBadge
        ? [
            {
              header: "Source",
              id: "source",
              cell: ({ row: { original } }) => (
                <SourceBadge
                  row={original}
                  labels={labels}
                  arrNames={arrNames}
                  defaultArr={defaultArr}
                  preferSnapshot={
                    original.origin === "source" && original.item_id === null
                  }
                />
              ),
            } satisfies ColumnDef<History.MovieRow>,
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
            return (
              <Badge>
                <Language.Text value={language} long></Language.Text>
              </Badge>
            );
          } else {
            return null;
          }
        },
      },
      {
        header: "Provider",
        accessorKey: "provider",
        cell: ({ row: { original } }) => (
          <HistoryProvider
            provider={original.provider}
            aiTranslated={original.ai_translated}
            action={original.action}
          />
        ),
      },
      {
        header: "Score",
        accessorKey: "score",
      },
      {
        header: "Match",
        accessorKey: "matches",
        cell: (row) => {
          const { matches, dont_matches: dont } = row.row.original;
          if (matches.length || dont.length) {
            return (
              <StateIcon
                matches={matches}
                dont={dont}
                isHistory={true}
              ></StateIcon>
            );
          } else {
            return null;
          }
        },
      },
      {
        header: "Date",
        accessorKey: "timestamp",
        cell: ({
          row: {
            original: { timestamp, parsed_timestamp },
          },
        }) => {
          if (timestamp) {
            return (
              <TextPopover text={parsed_timestamp}>
                <Text>{timestamp}</Text>
              </TextPopover>
            );
          } else {
            return null;
          }
        },
      },
      {
        header: "Info",
        accessorKey: "description",
        cell: ({ row: { original } }) => <HistoryInfo row={original} />,
      },
      {
        header: "Upgradable",
        accessorKey: "upgradable",
        cell: ({ row: { original } }) => (
          <HistoryUpgrade row={original} anchors={anchors} />
        ),
      },
      {
        header: "Excluded",
        accessorKey: "blacklisted",
        cell: ({ row }) => {
          if (row.original.origin === "source") {
            return row.original.blacklisted ? (
              <Text size="sm">Excluded</Text>
            ) : null;
          }
          const {
            blacklisted,
            radarrId,
            provider,
            subs_id,
            language,
            subtitles_path,
            arr_instance_id,
          } = row.original;

          if (subs_id && provider && language) {
            return (
              <MutateAction
                label="Exclude"
                disabled={blacklisted}
                icon={faFileExcel}
                mutation={addToBlacklist}
                args={() => ({
                  id: radarrId,
                  form: {
                    provider,
                    subs_id,
                    subtitles_path,
                    language: language.code2,
                    arr_instance_id,
                  },
                })}
              ></MutateAction>
            );
          } else {
            return null;
          }
        },
      },
    ],
    [addToBlacklist, anchors, arrNames, defaultArr, labels, showBadge],
  );

  return (
    <HistoryView
      name="Movies"
      query={query}
      columns={columns}
      toolbar={
        <Group justify="flex-end" mb="xs">
          <SourceFilter
            labels={labels}
            value={selected}
            onChange={sources.onChange}
            showSingle={!sources.sourceOnly}
          />
          <Switch
            label="Show Embedded Source records"
            checked={includeEmbedded}
            onChange={(event) =>
              setIncludeEmbedded(event.currentTarget.checked)
            }
          ></Switch>
        </Group>
      }
    ></HistoryView>
  );
};

export default MoviesHistoryView;
