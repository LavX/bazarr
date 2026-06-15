import React, { FunctionComponent, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import { Badge, Group, Text, TextProps, Tooltip } from "@mantine/core";
import {
  faEllipsis,
  faQuestionCircle,
  faSpinner,
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

export function buildMovieSubtitleToolSelections(
  movie: Item.Movie | null,
  subtitle: Subtitle,
): FormType.ModifySubtitle[] {
  const { code2, path, hi, forced } = subtitle;
  if (isSubtitleMissing(path) || movie === null) {
    return [];
  }

  const isEmbedded = isSubtitleTrack(path);
  return [
    {
      type: "movie",
      path: isEmbedded ? "" : path!,
      id: movie.radarrId,
      language: code2,
      forced: toPython(forced),
      hi: toPython(hi),
      from_language: isEmbedded ? code2 : undefined,
      arr_instance_id: movie.arr_instance_id,
    },
  ];
}

function subtitleEditorUrl(
  action: "preview" | "edit",
  mediaType: "movie",
  mediaId: number,
  language: string,
  arrInstanceId?: number | null,
) {
  const path = `/subtitles/${action}/${mediaType}/${mediaId}/${encodeURIComponent(language)}`;
  return arrInstanceId != null
    ? `${path}?arr_instance_id=${arrInstanceId}`
    : path;
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
  arrInstanceId?: number | null;
  subtitle: Subtitle;
}> = ({ actions, mediaId, arrInstanceId, subtitle }) => {
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
    arrInstanceId ?? undefined,
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

function buildLanguageKey(sub: {
  code2: string;
  hi?: boolean;
  forced?: boolean;
}): string {
  let key = sub.code2;
  if (sub.hi) key += ":hi";
  if (sub.forced) key += ":forced";
  return key;
}

// Key for the embedded action=7 score lookup. The backend stores embedded history
// with a single hi-priority modifier (ProcessSubtitlesResult drops forced when hi is
// set), so a hi+forced track is recorded as "<code>:hi". Mirror that here so the row
// lookup matches the stored history; do NOT use buildLanguageKey (which keeps both
// modifiers and is needed for unique row ids).
function buildEmbeddedScoreKey(sub: {
  code2: string;
  hi?: boolean;
  forced?: boolean;
}): string {
  if (sub.hi) return `${sub.code2}:hi`;
  if (sub.forced) return `${sub.code2}:forced`;
  return sub.code2;
}

const Table: FunctionComponent<Props> = ({ movie, profile, history }) => {
  const onlyDesired = useShowOnlyDesired();
  const [compareSelection, setCompareSelection] = useState<{
    original: Subtitle;
    outputs: Subtitle[];
  } | null>(null);

  const profileItems = useProfileItemsToLanguages(profile);

  const { download, remove } = useMovieSubtitleModification();
  const combine = useCombineSubtitles();

  // Available subtitles for translate-from source, include embedded tracks
  // (backend handles bitmap exclusion at extraction time)
  const availableSources = useMemo(
    () =>
      // Embedded tracks (empty path) are valid translate sources, so do not
      // filter on s.path / isSubtitleTrack; only exclude sync- and combined-output.
      (movie?.subtitles ?? []).filter(
        (s) => !isSyncOutputSubtitle(s) && !isCombinedOutputSubtitle(s),
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

  const embeddedScoreMap = useMemo(() => {
    const map = new Map<string, History.Movie>();
    history?.forEach((h) => {
      if (h.action === 7 && h.language?.code2) {
        const key = buildEmbeddedScoreKey(h.language);
        if (!map.has(key)) map.set(key, h);
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
      return buildMovieSubtitleToolSelections(movie, item);
    }, [item, movie]);

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
                scope: {
                  kind: "movie",
                  radarrId,
                  arrInstanceId: movie.arr_instance_id ?? undefined,
                },
                body: combineRequestForSubtitle(item) ?? {},
              });
            } else if (action === "view") {
              navigate(
                subtitleEditorUrl(
                  "preview",
                  "movie",
                  radarrId,
                  buildSubtitleLanguageKey(item),
                  movie.arr_instance_id,
                ),
              );
            } else if (action === "edit") {
              navigate(
                subtitleEditorUrl(
                  "edit",
                  "movie",
                  radarrId,
                  buildSubtitleLanguageKey(item),
                  movie.arr_instance_id,
                ),
              );
            } else if (action === "delete" && path) {
              await remove.mutateAsync({
                radarrId,
                arrInstanceId: movie.arr_instance_id,
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
          arrInstanceId={movie.arr_instance_id}
          onAction={async (action) => {
            if (action === "edit") {
              navigate(
                subtitleEditorUrl(
                  "edit",
                  "movie",
                  radarrId,
                  buildSubtitleLanguageKey(item),
                  movie.arr_instance_id,
                ),
              );
            } else if (action === "search") {
              await download.mutateAsync({
                radarrId,
                arrInstanceId: movie.arr_instance_id,
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
        embeddedTrack={isSubtitleTrack(path)}
        canSync={canSynchronizeSubtitle(item)}
        canCompareSyncOutputs={canCompareSyncOutputs}
        onAction={async (action) => {
          if (action === "view") {
            navigate(
              subtitleEditorUrl(
                "preview",
                "movie",
                radarrId,
                buildSubtitleLanguageKey(item),
                movie.arr_instance_id,
              ),
            );
          } else if (action === "edit") {
            navigate(
              subtitleEditorUrl(
                "edit",
                "movie",
                radarrId,
                buildSubtitleLanguageKey(item),
                movie.arr_instance_id,
              ),
            );
          } else if (action === "compare-sync") {
            setCompareSelection({ original: item, outputs: syncOutputs });
          } else if (action === "delete" && path) {
            await remove.mutateAsync({
              radarrId,
              arrInstanceId: movie.arr_instance_id,
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
        <Action label="Subtitle Actions" icon={faEllipsis}></Action>
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
          const record = !isSubtitleTrack(original.path)
            ? historyMap.get(original.path!)
            : embeddedScoreMap.get(buildEmbeddedScoreKey(original));
          return <ScoreBadge score={record?.score} />;
        },
      },
      {
        id: "provider",
        header: "Provider",
        cell: ({ row: { original } }) => {
          const record = !isSubtitleTrack(original.path)
            ? historyMap.get(original.path!)
            : embeddedScoreMap.get(buildEmbeddedScoreKey(original));
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
          const actions = !isSubtitleTrack(original.path)
            ? statusMap.get(original.path!)
            : undefined;
          if (!actions?.size)
            return (
              <Text c="dimmed" size="xs">
                -
              </Text>
            );
          return (
            <SubtitleStatusCell
              actions={actions}
              mediaId={movie?.radarrId}
              arrInstanceId={movie?.arr_instance_id}
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
    [
      CodeCell,
      historyMap,
      embeddedScoreMap,
      movie?.radarrId,
      movie?.arr_instance_id,
      statusMap,
    ],
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
        getRowId={(sub) => {
          // Missing rows all share the sentinel path "Missing Subtitles" and
          // embedded tracks have an empty path, so both must be keyed by their
          // language (code2[:hi][:forced]) to stay unique. Only real on-disk
          // subtitles can safely use the file path as the row id.
          if (isSubtitleMissing(sub.path)) {
            return `missing-${buildLanguageKey(sub)}`;
          }
          if (isSubtitleTrack(sub.path)) {
            return `embedded-${buildLanguageKey(sub)}`;
          }
          return sub.path!;
        }}
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
