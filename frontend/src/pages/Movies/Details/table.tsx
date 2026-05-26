import React, { FunctionComponent, useMemo } from "react";
import { useNavigate } from "react-router";
import { Badge, Group, Text, TextProps } from "@mantine/core";
import { faEllipsis } from "@fortawesome/free-solid-svg-icons";
import { ColumnDef } from "@tanstack/react-table";
import { isString } from "lodash";
import { useMovieSubtitleModification } from "@/apis/hooks";
import { useShowOnlyDesired } from "@/apis/hooks/site";
import { Action } from "@/components";
import { HistoryIcon } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import SimpleTable from "@/components/tables/SimpleTable";
import { filterSubtitleBy, toPython } from "@/utilities";
import { useProfileItemsToLanguages } from "@/utilities/languages";

const missingText = "Missing Subtitles";

interface Props {
  movie: Item.Movie | null;
  disabled?: boolean;
  profile?: Language.Profile;
  history?: History.Movie[];
}

const ScoreBadge: React.FC<{ score?: string | number | null }> = ({
  score,
}) => {
  if (score === undefined || score === null || score === "") {
    return (
      <Text c="dimmed" size="xs">
        —
      </Text>
    );
  }
  const pct = typeof score === "string" ? parseFloat(score) : score;
  const color = pct >= 90 ? "green" : pct >= 70 ? "yellow" : "red";
  return (
    <Badge color={color} size="sm" variant="light">
      {typeof score === "number" ? `${score}%` : score}
    </Badge>
  );
};

function isSubtitleTrack(path: string | undefined | null) {
  return !isString(path) || path.length === 0;
}

function isSubtitleMissing(path: string | undefined | null) {
  return path === missingText;
}

function buildLanguageKey(sub: Subtitle): string {
  let key = sub.code2;
  if (sub.hi) key += ":hi";
  if (sub.forced) key += ":forced";
  return key;
}

const Table: FunctionComponent<Props> = ({ movie, profile, history }) => {
  const onlyDesired = useShowOnlyDesired();

  const profileItems = useProfileItemsToLanguages(profile);

  const { download, remove } = useMovieSubtitleModification();

  // Available subtitles with actual files (for translate-from source)
  const availableSources = useMemo(
    () =>
      (movie?.subtitles ?? []).filter(
        (s) => s.path && !isSubtitleTrack(s.path),
      ),
    [movie?.subtitles],
  );

  const historyMap = useMemo(() => {
    const map = new Map<string, History.Movie>();
    history?.forEach((h) => {
      if (!h.subtitles_path) return;
      if ([1, 2, 3].includes(h.action)) {
        if (!map.has(h.subtitles_path)) map.set(h.subtitles_path, h);
      }
    });
    return map;
  }, [history]);

  const statusMap = useMemo(() => {
    const map = new Map<string, Set<number>>();
    history?.forEach((h) => {
      if (!h.subtitles_path) return;
      if ([5, 6].includes(h.action)) {
        if (!map.has(h.subtitles_path)) map.set(h.subtitles_path, new Set());
        map.get(h.subtitles_path)!.add(h.action);
      }
    });
    return map;
  }, [history]);

  const navigate = useNavigate();

  const CodeCell = React.memo(({ item }: { item: Subtitle }) => {
    const { code2, path, hi, forced } = item;

    const selections = useMemo(() => {
      const list: FormType.ModifySubtitle[] = [];

      if (path && !isSubtitleMissing(path) && movie !== null) {
        list.push({
          type: "movie",
          path,
          id: movie.radarrId,
          language: code2,
          forced: toPython(forced),
          hi: toPython(hi),
        });
      }

      return list;
    }, [code2, path, forced, hi]);

    if (movie === null) {
      return null;
    }

    const { radarrId } = movie;

    if (isSubtitleMissing(path)) {
      return (
        <SubtitleToolsMenu
          selections={[]}
          missingLanguage={item}
          translationSources={availableSources}
          mediaId={radarrId}
          mediaType="movie"
          onAction={async (action) => {
            if (action === "edit") {
              navigate(
                `/subtitles/edit/movie/${radarrId}/${encodeURIComponent(buildLanguageKey(item))}`,
              );
            } else if (action === "search") {
              await download.mutateAsync({
                radarrId,
                form: {
                  language: code2,
                  forced,
                  hi,
                },
              });
            }
          }}
        >
          <Action label="Subtitle Actions" icon={faEllipsis}></Action>
        </SubtitleToolsMenu>
      );
    }

    return (
      <SubtitleToolsMenu
        selections={selections}
        onAction={async (action) => {
          if (action === "view") {
            navigate(
              `/subtitles/preview/movie/${radarrId}/${encodeURIComponent(buildLanguageKey(item))}`,
            );
          } else if (action === "edit") {
            navigate(
              `/subtitles/edit/movie/${radarrId}/${encodeURIComponent(buildLanguageKey(item))}`,
            );
          } else if (action === "delete" && path) {
            await remove.mutateAsync({
              radarrId,
              form: {
                language: code2,
                forced,
                hi,
                path,
              },
            });
          }
        }}
      >
        <Action
          label="Subtitle Actions"
          disabled={isSubtitleTrack(path)}
          icon={faEllipsis}
        ></Action>
      </SubtitleToolsMenu>
    );
  });

  const columns = useMemo<ColumnDef<Subtitle>[]>(
    () => [
      {
        header: "Subtitle Path",
        accessorKey: "path",
        cell: ({
          row: {
            original: { path },
          },
        }) => {
          const props: TextProps = {
            className: "table-primary",
          };

          if (isSubtitleTrack(path)) {
            return (
              <Text className="table-primary">Video File Subtitle Track</Text>
            );
          } else if (isSubtitleMissing(path)) {
            return (
              <Text {...props} c="var(--bz-text-tertiary)">
                {path}
              </Text>
            );
          } else {
            return <Text {...props}>{path}</Text>;
          }
        },
      },
      {
        header: "Language",
        accessorKey: "name",
        cell: ({ row }) => {
          if (row.original.path === missingText) {
            return (
              <Badge variant="missing">
                <Language.Text value={row.original} long></Language.Text>
              </Badge>
            );
          } else {
            return (
              <Badge>
                <Language.Text value={row.original} long></Language.Text>
              </Badge>
            );
          }
        },
      },
      {
        id: "score",
        header: "Score",
        cell: ({ row: { original } }) => {
          const record = historyMap.get(original.path ?? "");
          return <ScoreBadge score={record?.score} />;
        },
      },
      {
        id: "provider",
        header: "Provider",
        cell: ({ row: { original } }) => {
          const record = historyMap.get(original.path ?? "");
          if (!record?.provider)
            return (
              <Text c="dimmed" size="xs">
                —
              </Text>
            );
          return <Text size="xs">{record.provider}</Text>;
        },
      },
      {
        id: "status",
        header: "Status",
        cell: ({ row: { original } }) => {
          const actions = statusMap.get(original.path ?? "");
          if (!actions?.size)
            return (
              <Text c="dimmed" size="xs">
                —
              </Text>
            );
          return (
            <Group gap={4}>
              {Array.from(actions).map((action) => (
                <HistoryIcon key={action} action={action} />
              ))}
            </Group>
          );
        },
      },
      {
        id: "code2",
        cell: ({ row: { original } }) => {
          return <CodeCell item={original} />;
        },
      },
    ],
    [CodeCell, historyMap, statusMap],
  );

  const data: Subtitle[] = useMemo(() => {
    const missing =
      movie?.missing_subtitles.map((item) => ({
        ...item,
        path: missingText,
      })) ?? [];

    let subtitles = movie?.subtitles ?? [];
    if (onlyDesired) {
      subtitles = filterSubtitleBy(subtitles, profileItems);
    }

    return [...subtitles, ...missing];
  }, [movie, onlyDesired, profileItems]);

  return (
    <SimpleTable
      columns={columns}
      data={data}
      tableStyles={{ emptyText: "No subtitles found for this movie" }}
    ></SimpleTable>
  );
};

export default Table;
