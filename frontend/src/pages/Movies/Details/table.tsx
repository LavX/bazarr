import React, { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  Badge,
  Button,
  Group,
  Menu,
  Text,
  TextProps,
  Tooltip,
} from "@mantine/core";
import {
  faEllipsis,
  faEye,
  faQuestionCircle,
  faRotateRight,
  faSpinner,
  faTrash,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef } from "@tanstack/react-table";
import { isString } from "lodash";
import {
  useMovieSubtitleModification,
  useSubtitleSyncStatus,
} from "@/apis/hooks";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useShowOnlyDesired } from "@/apis/hooks/site";
import { Action } from "@/components";
import { CombinedSubtitleBadge, HistoryIcon } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import SimpleTable from "@/components/tables/SimpleTable";
import { filterSubtitleBy, toPython } from "@/utilities";
import { useProfileItemsToLanguages } from "@/utilities/languages";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  getSubtitleSyncStatusPresentation,
  getSyncEngineLabel,
  isCombinedOutputSubtitle,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputSubtitle,
  sortSyncOutputSubtitles,
} from "@/utilities/subtitles";

const missingText = "Missing Subtitles";
const syncAction = 5;

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
        -
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

const SyncEngineBadge: FunctionComponent<{ subtitle: Subtitle }> = ({
  subtitle,
}) => {
  if (!isSyncOutputSubtitle(subtitle)) {
    return null;
  }

  return (
    <Badge
      color="gray"
      size="xs"
      variant="light"
      style={{ whiteSpace: "nowrap" }}
    >
      {getSyncEngineLabel(subtitle.modifier)}
    </Badge>
  );
};

const SubtitleLanguageBadges: FunctionComponent<{
  subtitle: Subtitle;
  missing?: boolean;
}> = ({ subtitle, missing = false }) => {
  if (missing) {
    return (
      <Badge variant="missing" style={{ whiteSpace: "nowrap" }}>
        <Language.Text value={subtitle} long></Language.Text>
      </Badge>
    );
  }

  if (isCombinedOutputSubtitle(subtitle)) {
    return <CombinedSubtitleBadge subtitle={subtitle} />;
  }

  return (
    <Group gap={4} wrap="nowrap">
      <Badge style={{ whiteSpace: "nowrap" }}>
        <Language.Text value={subtitle} long></Language.Text>
      </Badge>
      <SyncEngineBadge subtitle={subtitle} />
    </Group>
  );
};

const UnconfirmedSyncIcon: FunctionComponent<{ label: string }> = ({
  label,
}) => (
  <Tooltip
    label={label}
    openDelay={500}
    position="right"
    events={{ hover: true, focus: false, touch: true }}
  >
    <Text span c="yellow.5">
      <FontAwesomeIcon
        aria-label="Sync unconfirmed"
        icon={faQuestionCircle}
      ></FontAwesomeIcon>
    </Text>
  </Tooltip>
);

const ActiveSyncIcon: FunctionComponent<{ label: string }> = ({ label }) => (
  <Tooltip
    label={label}
    openDelay={500}
    position="right"
    events={{ hover: true, focus: false, touch: true }}
  >
    <Text span c="blue.4">
      <FontAwesomeIcon
        aria-label={label}
        icon={faSpinner}
        spin
      ></FontAwesomeIcon>
    </Text>
  </Tooltip>
);

const SubtitleStatusCell: FunctionComponent<{
  actions: Set<number> | undefined;
  mediaId: number | undefined;
  subtitle: Subtitle;
}> = ({ actions, mediaId, subtitle }) => {
  const languageKey = buildSubtitleLanguageKey(subtitle);
  const canCheckSyncStatus =
    !!subtitle.path &&
    !isSubtitleTrack(subtitle.path) &&
    !isSubtitleMissing(subtitle.path) &&
    !isCombinedOutputSubtitle(subtitle);
  const syncStatus = useSubtitleSyncStatus(
    "movie",
    mediaId,
    languageKey,
    canCheckSyncStatus,
  );
  const presentation = syncStatus.data
    ? getSubtitleSyncStatusPresentation(syncStatus.data)
    : null;

  if (!actions?.size) {
    if (presentation?.icon === "running") {
      return <ActiveSyncIcon label={presentation.label} />;
    }

    return (
      <Text c="dimmed" size="xs">
        -
      </Text>
    );
  }

  return (
    <Group gap={4} wrap="nowrap">
      {Array.from(actions).map((action) => {
        if (action !== syncAction) {
          return <HistoryIcon key={action} action={action} />;
        }

        if (syncStatus.isError) {
          return (
            <UnconfirmedSyncIcon
              key={action}
              label="Sync status could not be verified"
            />
          );
        }

        if (presentation?.icon === "running") {
          return <ActiveSyncIcon key={action} label={presentation.label} />;
        }

        if (presentation?.icon === "question") {
          return (
            <UnconfirmedSyncIcon key={action} label={presentation.label} />
          );
        }

        return <HistoryIcon key={action} action={action} />;
      })}
    </Group>
  );
};

const Table: FunctionComponent<Props> = ({ movie, profile, history }) => {
  const onlyDesired = useShowOnlyDesired();
  const [compareSelection, setCompareSelection] = useState<{
    original: Subtitle;
    outputs: Subtitle[];
  } | null>(null);

  const profileItems = useProfileItemsToLanguages(profile);

  const { download, remove } = useMovieSubtitleModification();
  const combine = useCombineSubtitles();

  // Available subtitles with actual files (for translate-from source)
  const availableSources = useMemo(
    () =>
      (movie?.subtitles ?? []).filter(
        (s) => s.path && !isSubtitleTrack(s.path) && !isSyncOutputSubtitle(s),
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
    const syncOutputs = sortSyncOutputSubtitles(
      (movie.subtitles ?? []).filter((subtitle) =>
        isCompatibleSyncOutputSubtitle(item, subtitle),
      ),
    );
    const canCompareSyncOutputs =
      syncOutputs.length > 0 &&
      !isSyncOutputSubtitle(item) &&
      !!path &&
      !isSubtitleMissing(path);

    if (isCombinedOutputSubtitle(item)) {
      return (
        <SubtitleToolsMenu
          selections={selections}
          isCombinedOutput
          onAction={async (action) => {
            if (action === "rebuild") {
              combine.mutate({
                scope: { kind: "movie", radarrId },
                body: combineRequestForSubtitle(item) ?? {},
              });
            } else if (action === "view") {
              navigate(
                `/subtitles/preview/movie/${radarrId}/${encodeURIComponent(buildSubtitleLanguageKey(item))}`,
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
          <Action label="Combined Subtitle Actions" icon={faEllipsis} />
        </SubtitleToolsMenu>
      );
    }

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
                `/subtitles/edit/movie/${radarrId}/${encodeURIComponent(buildSubtitleLanguageKey(item))}`,
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
        canSync={canSynchronizeSubtitle(item)}
        canCompareSyncOutputs={canCompareSyncOutputs}
        onAction={async (action) => {
          if (action === "view") {
            navigate(
              `/subtitles/preview/movie/${radarrId}/${encodeURIComponent(buildSubtitleLanguageKey(item))}`,
            );
          } else if (action === "edit") {
            navigate(
              `/subtitles/edit/movie/${radarrId}/${encodeURIComponent(buildSubtitleLanguageKey(item))}`,
            );
          } else if (action === "compare-sync") {
            setCompareSelection({ original: item, outputs: syncOutputs });
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
          return (
            <SubtitleLanguageBadges
              subtitle={row.original}
              missing={row.original.path === missingText}
            />
          );
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
                -
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
          return (
            <SubtitleStatusCell
              actions={actions}
              mediaId={movie?.radarrId}
              subtitle={original}
            />
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
    [CodeCell, historyMap, movie?.radarrId, statusMap],
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
    <>
      <SimpleTable
        columns={columns}
        data={data}
        tableStyles={{ emptyText: "No subtitles found for this movie" }}
      ></SimpleTable>
      {movie && compareSelection && (
        <SyncOutputCompareModal
          opened={compareSelection !== null}
          onClose={() => setCompareSelection(null)}
          mediaType="movie"
          mediaId={movie.radarrId}
          original={compareSelection.original}
          outputs={compareSelection.outputs}
        />
      )}
    </>
  );
};

export default Table;
