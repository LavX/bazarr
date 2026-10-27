import {
  createContext,
  Dispatch,
  FunctionComponent,
  SetStateAction,
  useContext,
  useMemo,
  useState,
} from "react";
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
  useEmbeddedTrackVisibility,
  useMovieSubtitleModification,
  useSubtitleFileDownload,
  useSubtitleSyncStatus,
} from "@/apis/hooks";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useShowOnlyDesired } from "@/apis/hooks/site";
import { Action } from "@/components";
import { CombinedSubtitleBadge, HistoryIcon } from "@/components/bazarr";
import Language from "@/components/bazarr/Language";
import HiddenTracks from "@/components/HiddenTracks";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import SimpleTable from "@/components/tables/SimpleTable";
import { subtitleEditorUrl } from "@/pages/SubtitleEditor/editorScope";
import { filterSubtitleBy, toPython } from "@/utilities";
import { basename } from "@/utilities/files";
import { useProfileItemsToLanguages } from "@/utilities/languages";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  describeEmbeddedTrack,
  embeddedTrackLabel,
  embeddedTracksSharingKey,
  getSubtitleSyncStatusPresentation,
  getSyncEngineLabel,
  isBitmapCodec,
  isCombinedOutputSubtitle,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputSubtitle,
  sortSyncOutputSubtitles,
  subtitleCodecLabel,
} from "@/utilities/subtitles";
import styles from "./table.module.scss";

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

// A copy of the history, newest first. ISO timestamps retain the database's
// microsecond precision. Display timestamps are localized and cannot be used
// to order file mutations.
function newestFirst(history: History.Movie[] | undefined) {
  return [...(history ?? [])].sort((a, b) => {
    if (
      a.timestamp_iso &&
      b.timestamp_iso &&
      a.timestamp_iso !== b.timestamp_iso
    ) {
      return a.timestamp_iso > b.timestamp_iso ? -1 : 1;
    }
    return (b.history_id ?? 0) - (a.history_id ?? 0);
  });
}

// Deletion, the downloads and upgrades, upload, translation and extraction
// from an embedded track (8) each put a different file at the path, or none.
// Sync (5) rewrites the same file and embedded tracks (7) have no path.
function replacesFile(action: number) {
  return [0, 1, 2, 3, 4, 6, 8].includes(action);
}

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

// History stores a single hi-priority modifier: ProcessSubtitlesResult drops
// forced when hi is set. Match that for history lookups, while row IDs retain
// both flags through buildLanguageKey.
function buildHistoryLanguageKey(sub: {
  code2: string;
  hi?: boolean;
  forced?: boolean;
}): string {
  if (sub.hi) return `${sub.code2}:hi`;
  if (sub.forced) return `${sub.code2}:forced`;
  return sub.code2;
}

// An Embedded Source row records a forced and HI track as forced, so a track
// with both flags is looked up under that key first. The HI key follows for the
// rows an older version wrote.
function embeddedHistoryLanguageKeys(sub: {
  code2: string;
  hi?: boolean;
  forced?: boolean;
}): string[] {
  return sub.hi && sub.forced
    ? [`${sub.code2}:forced`, `${sub.code2}:hi`]
    : [buildHistoryLanguageKey(sub)];
}

type CompareSelection = { original: Subtitle; outputs: Subtitle[] } | null;

// What the cells read from the table. The cells and the columns live at module
// scope and take the movie from here: a cell component declared inside the
// table's render is a new component type on every render, so each refetch
// remounted every cell and closed any menu a row had open, even on a row whose
// own subtitle had not changed.
interface MovieTableValue {
  movie: Item.Movie | null;
  disabled?: boolean;
  availableSources: Subtitle[];
  historyMap: Map<string, History.Movie>;
  embeddedScoreMap: Map<string, History.Movie>;
  statusMap: Map<string, Set<number>>;
  setCompareSelection: Dispatch<SetStateAction<CompareSelection>>;
}

const MovieTableContext = createContext<MovieTableValue | null>(null);

function useMovieTable(): MovieTableValue {
  const value = useContext(MovieTableContext);
  if (value === null) {
    throw new Error("Movie subtitle cells render only inside the table");
  }
  return value;
}

function historyRecordFor(
  subtitle: Subtitle,
  historyMap: Map<string, History.Movie>,
  embeddedScoreMap: Map<string, History.Movie>,
) {
  if (!isSubtitleTrack(subtitle.path)) return historyMap.get(subtitle.path!);
  for (const key of embeddedHistoryLanguageKeys(subtitle)) {
    const record = embeddedScoreMap.get(key);
    if (record) return record;
  }
  return undefined;
}

const ScoreCell: FunctionComponent<{ subtitle: Subtitle }> = ({ subtitle }) => {
  const { historyMap, embeddedScoreMap } = useMovieTable();
  const record = historyRecordFor(subtitle, historyMap, embeddedScoreMap);
  return <ScoreBadge score={record?.score} />;
};

const ProviderCell: FunctionComponent<{ subtitle: Subtitle }> = ({
  subtitle,
}) => {
  const { historyMap, embeddedScoreMap } = useMovieTable();
  const record = historyRecordFor(subtitle, historyMap, embeddedScoreMap);
  if (!record?.provider)
    return (
      <Text c="dimmed" size="xs">
        -
      </Text>
    );
  return <Text size="xs">{record.provider}</Text>;
};

const StatusCell: FunctionComponent<{ subtitle: Subtitle }> = ({
  subtitle,
}) => {
  const { movie, statusMap } = useMovieTable();
  const actions = !isSubtitleTrack(subtitle.path)
    ? statusMap.get(
        JSON.stringify([subtitle.path, buildHistoryLanguageKey(subtitle)]),
      )
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
      subtitle={subtitle}
    />
  );
};

const CodeCell: FunctionComponent<{ item: Subtitle }> = ({ item }) => {
  const { movie, disabled, availableSources, setCompareSelection } =
    useMovieTable();
  const navigate = useNavigate();
  const { download, remove } = useMovieSubtitleModification();
  const { hide } = useEmbeddedTrackVisibility();
  const combine = useCombineSubtitles();
  const fileDownload = useSubtitleFileDownload();

  const { code2, path, hi, forced } = item;

  // View, Edit and Download of an embedded row read the track inside the
  // video; nothing is written next to it until the editor saves. The row's
  // codec picks between two text tracks that share a language key.
  const source = isSubtitleTrack(path) ? ("embedded" as const) : undefined;
  const codec = source ? (item.codec ?? undefined) : undefined;

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
                { arrInstanceId: movie.arr_instance_id },
              ),
            );
          } else if (action === "edit") {
            navigate(
              subtitleEditorUrl(
                "edit",
                "movie",
                radarrId,
                buildSubtitleLanguageKey(item),
                { arrInstanceId: movie.arr_instance_id },
              ),
            );
          } else if (action === "download") {
            fileDownload.mutate({
              type: "movie",
              mediaId: radarrId,
              language: buildSubtitleLanguageKey(item),
              arrInstanceId: movie.arr_instance_id,
            });
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
        <Action
          label="Combined Subtitle Actions"
          icon={faEllipsis}
          disabled={disabled}
        />
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
                { arrInstanceId: movie.arr_instance_id },
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
        <Action
          label="Subtitle Actions"
          icon={faEllipsis}
          disabled={disabled}
        ></Action>
      </SubtitleToolsMenu>
    );
  }

  return (
    <SubtitleToolsMenu
      selections={selections}
      embeddedTrack={
        isSubtitleTrack(path) ? { codec: item.codec ?? null } : undefined
      }
      trackLabel={embeddedTrackLabel(item)}
      coveredTracks={
        isSubtitleTrack(path)
          ? embeddedTracksSharingKey(item, movie.subtitles).map((track) =>
              embeddedTrackLabel(track),
            )
          : undefined
      }
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
              { arrInstanceId: movie.arr_instance_id, source, codec },
            ),
          );
        } else if (action === "edit") {
          navigate(
            subtitleEditorUrl(
              "edit",
              "movie",
              radarrId,
              buildSubtitleLanguageKey(item),
              { arrInstanceId: movie.arr_instance_id, source, codec },
            ),
          );
        } else if (action === "compare-sync") {
          setCompareSelection({ original: item, outputs: syncOutputs });
        } else if (action === "download") {
          fileDownload.mutate({
            type: "movie",
            mediaId: radarrId,
            language: buildSubtitleLanguageKey(item),
            arrInstanceId: movie.arr_instance_id,
            source,
            codec,
          });
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
        } else if (action === "hide" && isSubtitleTrack(path)) {
          // mutate, not mutateAsync: the hook reports a refusal, and the
          // menu does not await this callback.
          hide.mutate({
            /* eslint-disable camelcase */
            media_type: "movie",
            media_id: movie.id,
            arr_instance_id: movie.arr_instance_id,
            /* eslint-enable camelcase */
            language: code2,
            hi: toPython(hi),
            forced: toPython(forced),
          });
        }
      }}
    >
      <Action
        label="Subtitle Actions"
        icon={faEllipsis}
        disabled={disabled}
      ></Action>
    </SubtitleToolsMenu>
  );
};

const columns: ColumnDef<Subtitle>[] = [
  {
    header: "Subtitle Path",
    accessorKey: "path",
    meta: { className: styles.pathCell },
    cell: ({
      row: {
        original: { path, codec },
      },
    }) => {
      const props: TextProps = {
        className: "table-primary",
      };

      if (isSubtitleTrack(path)) {
        const codecLabel = subtitleCodecLabel(codec);
        return (
          <Group gap={6} wrap="nowrap">
            <Text className="table-primary">Video File Subtitle Track</Text>
            {codecLabel && (
              <Badge
                size="xs"
                variant="light"
                color={isBitmapCodec(codec) ? "gray" : undefined}
                title={describeEmbeddedTrack("Subtitle format", codec)}
              >
                {codecLabel}
              </Badge>
            )}
          </Group>
        );
      } else if (isSubtitleMissing(path)) {
        return (
          <Text {...props} c="var(--bz-text-tertiary)">
            {path}
          </Text>
        );
      } else {
        // Real file rows: show only the file name and reveal the full path
        // in a hover tooltip. The .pathCell ellipsis still handles very long
        // file names.
        return (
          <Tooltip
            label={path}
            multiline
            w={480}
            maw="90vw"
            style={{ overflowWrap: "anywhere" }}
            events={{ hover: true, focus: false, touch: true }}
          >
            <Text {...props}>{basename(path!)}</Text>
          </Tooltip>
        );
      }
    },
  },
  {
    header: "Language",
    accessorKey: "name",
    meta: { className: styles.fixedCell },
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
    meta: { className: styles.fixedCell },
    cell: ({ row: { original } }) => <ScoreCell subtitle={original} />,
  },
  {
    id: "provider",
    header: "Provider",
    meta: { className: styles.fixedCell },
    cell: ({ row: { original } }) => <ProviderCell subtitle={original} />,
  },
  {
    id: "status",
    header: "Status",
    meta: { className: styles.fixedCell },
    cell: ({ row: { original } }) => <StatusCell subtitle={original} />,
  },
  {
    id: "code2",
    meta: { className: styles.fixedCell },
    cell: ({ row: { original } }) => <CodeCell item={original} />,
  },
];

const Table: FunctionComponent<Props> = ({
  movie,
  profile,
  history,
  disabled,
}) => {
  const onlyDesired = useShowOnlyDesired();
  const [compareSelection, setCompareSelection] =
    useState<CompareSelection>(null);

  const profileItems = useProfileItemsToLanguages(profile);

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

  // Score and provider belong to whatever last wrote the file on disk. A
  // sync rewrites it in place and keeps where it came from, so only the
  // newest record that replaced or removed the file counts, and only a
  // download, manual download, upgrade or extraction (whose score is the
  // track's) has a score to show.
  const historyMap = useMemo(() => {
    const map = new Map<string, History.Movie>();
    const settled = new Set<string>();
    newestFirst(history).forEach((h) => {
      if (!h.subtitles_path || settled.has(h.subtitles_path)) return;
      if (!replacesFile(h.action)) return;
      settled.add(h.subtitles_path);
      if ([1, 2, 3, 8].includes(h.action)) map.set(h.subtitles_path, h);
    });
    return map;
  }, [history]);

  const embeddedScoreMap = useMemo(() => {
    const map = new Map<string, History.Movie>();
    history?.forEach((h) => {
      if (h.action === 7 && h.language?.code2) {
        const key = buildHistoryLanguageKey(h.language);
        if (!map.has(key)) map.set(key, h);
      }
    });
    return map;
  }, [history]);

  const statusMap = useMemo(() => {
    const map = new Map<string, Set<number>>();
    const completed = new Set<string>();
    newestFirst(history).forEach((record) => {
      if (!record.subtitles_path || record.id !== movie?.id) return;
      if (
        record.arr_instance_id != null &&
        movie?.arr_instance_id != null &&
        record.arr_instance_id !== movie.arr_instance_id
      )
        return;

      if (completed.has(record.subtitles_path)) return;
      if ([5, 6].includes(record.action) && record.language) {
        const key = JSON.stringify([
          record.subtitles_path,
          buildHistoryLanguageKey(record.language),
        ]);
        if (!map.has(key)) map.set(key, new Set());
        map.get(key)!.add(record.action);
      }
      // A replacement or deletion ends this file's history. Translation and
      // extraction also write a new file, so only syncs after them still apply.
      if (replacesFile(record.action)) completed.add(record.subtitles_path);
    });
    return map;
  }, [history, movie]);

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

  // One file can carry two tracks with the same key, an SRT and a PGS English
  // track on a remux. Numbering only the tracks that share a key keeps them
  // apart, while a row added or removed elsewhere leaves a track's id, and so
  // its mounted row, unchanged.
  const embeddedRowIds = useMemo(() => {
    const seen = new Map<string, number>();
    const ids = new Map<Subtitle, string>();
    for (const sub of data) {
      if (!isSubtitleTrack(sub.path)) continue;
      const key = buildLanguageKey(sub);
      const count = seen.get(key) ?? 0;
      seen.set(key, count + 1);
      ids.set(
        sub,
        count === 0 ? `embedded-${key}` : `embedded-${key}-${count}`,
      );
    }
    return ids;
  }, [data]);

  const tableValue = useMemo<MovieTableValue>(
    () => ({
      movie,
      disabled,
      availableSources,
      historyMap,
      embeddedScoreMap,
      statusMap,
      setCompareSelection,
    }),
    [
      movie,
      disabled,
      availableSources,
      historyMap,
      embeddedScoreMap,
      statusMap,
    ],
  );

  return (
    <MovieTableContext.Provider value={tableValue}>
      <SimpleTable
        className={styles.table}
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
            return embeddedRowIds.get(sub)!;
          }
          return sub.path!;
        }}
        tableStyles={{ emptyText: "No subtitles found for this movie" }}
      ></SimpleTable>
      {movie && (
        <HiddenTracks
          mediaType="movie"
          mediaId={movie.id}
          disabled={disabled}
          align="end"
        />
      )}
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
    </MovieTableContext.Provider>
  );
};

export default Table;
