import React, {
  createContext,
  forwardRef,
  useCallback,
  useContext,
  useEffect,
  useMemo,
} from "react";
import { Badge, Group, Popover, Text, Tooltip } from "@mantine/core";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faHistory,
  faLayerGroup,
  faMagnifyingGlass,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Row, Table as TableInstance } from "@tanstack/react-table";
import { useDownloadEpisodeSubtitles, useEpisodesProvider } from "@/apis/hooks";
import { useShowOnlyDesired } from "@/apis/hooks/site";
import { Action, GroupTable } from "@/components";
import { AudioList } from "@/components/bazarr";
import { CombineModal } from "@/components/forms/CombineForm";
import HiddenTracks from "@/components/HiddenTracks";
import { EpisodeHistoryModal } from "@/components/modals";
import { EpisodeSearchModal } from "@/components/modals/ManualSearchModal";
import TextPopover from "@/components/TextPopover";
import { useModals } from "@/modules/modals";
import { filterSubtitleBy } from "@/utilities";
import { useProfileItemsToLanguages } from "@/utilities/languages";
import {
  buildSubtitleLanguageKey,
  isCombinedOutputSubtitle,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";
import { Subtitle, SubtitleScoreInfo } from "./components";

/**
 * How many subtitle badges an episode row draws before the rest go behind a
 * count.
 *
 * A release can carry several dozen embedded tracks and Apple TV+ WEB-DLs run
 * past eighty, and the row used to draw every one of them in a single
 * nowrap line. Flex items shrink, so past a certain count each badge was
 * squeezed below the width of its own label and the column read as a row of
 * single letters that pushed the table wider than its container.
 *
 * Wrapping alone keeps them legible but lets one episode own the page, so the
 * row wraps up to this many and the remainder moves behind a count that opens.
 */
const MAX_CELL_SUBTITLES = 12;

function SubtitlesOverflow({
  count,
  children,
}: {
  count: number;
  children: React.ReactNode;
}) {
  return (
    <Popover position="bottom-start" withArrow shadow="md" withinPortal>
      <Popover.Target>
        <Badge
          variant="light"
          color="gray"
          style={{ cursor: "pointer", flexShrink: 0 }}
          aria-label={`Show the ${count} further subtitles on this episode`}
        >
          +{count}
        </Badge>
      </Popover.Target>
      <Popover.Dropdown>
        <Group gap="xs" wrap="wrap" maw={420}>
          {children}
        </Group>
      </Popover.Dropdown>
    </Popover>
  );
}
import tableStyles from "@/components/tables/BaseTable.module.scss";

interface Props {
  episodes: Item.Episode[] | null;
  // Whole-series episode history (one request per detail page); used to show a
  // match score inside each downloaded/embedded subtitle badge.
  history?: History.Episode[];
  disabled?: boolean;
  profile?: Language.Profile;
  onAllRowsExpandedChanged: (isAllRowsExpanded: boolean) => void;
}

// History stores a single hi-priority modifier: forced is dropped when hi is
// set. Match that for history lookups. Mirrors the movie detail table.
function buildHistoryLanguageKey(sub: {
  code2: string;
  hi?: boolean;
  forced?: boolean;
}): string {
  if (sub.hi) return `${sub.code2}:hi`;
  if (sub.forced) return `${sub.code2}:forced`;
  return sub.code2;
}

function scoreInfoFromHistory(
  record: History.Episode | undefined,
): SubtitleScoreInfo | undefined {
  if (!record?.score) return undefined;
  return {
    score: record.score,
    provider: record.provider,
    matches: record.matches,
    notMatches: record.dont_matches,
  };
}

// What the cells read from the table. The cells and the columns live at module
// scope and take these from here: a cell component declared inside the
// table's render is a new component type on every render, so each refetch
// remounted every cell and closed any badge menu an episode had open, even on
// an episode whose own subtitles had not changed.
interface EpisodesTableValue {
  disabled?: boolean;
  // Every episode's disclosure asks with this same list, so the page makes
  // one request for all of them.
  episodeIds: number[];
  onlyDesired: boolean;
  profileItems: ReturnType<typeof useProfileItemsToLanguages>;
  historyMap: Map<string, History.Episode>;
  embeddedScoreMap: Map<string, History.Episode>;
  download: (item: Item.Episode, result: SearchResultType) => Promise<void>;
}

const EpisodesTableContext = createContext<EpisodesTableValue | null>(null);

function useEpisodesTable(): EpisodesTableValue {
  const value = useContext(EpisodesTableContext);
  if (value === null) {
    throw new Error("Episode cells render only inside the episodes table");
  }
  return value;
}

const SubtitlesCell = React.memo(({ row }: { row: Row<Item.Episode> }) => {
  const episode = row.original;
  const {
    disabled,
    episodeIds,
    onlyDesired,
    profileItems,
    historyMap,
    embeddedScoreMap,
  } = useEpisodesTable();
  const seriesId = episode.sonarrSeriesId;

  const elements = useMemo(() => {
    const episodeId = episode.sonarrEpisodeId;

    // Match score for a subtitle, from this episode's history: file rows
    // by path, embedded tracks by language key.
    const scoreFor = (val: Subtitle): SubtitleScoreInfo | undefined => {
      if (!val.path) {
        return scoreInfoFromHistory(
          embeddedScoreMap.get(
            JSON.stringify([episode.id, buildHistoryLanguageKey(val)]),
          ),
        );
      }
      const record = historyMap.get(JSON.stringify([episode.id, val.path]));
      return record && [1, 2, 3].includes(record.action)
        ? scoreInfoFromHistory(record)
        : undefined;
    };

    // The API does not expose embedded track ids. Distinguish repeated tracks
    // by their occurrence within a language, keeping file keys independent of
    // the order of other subtitles.
    const occurrences = new Map<string, number>();
    const subtitleKey = (val: Subtitle, kind: string) => {
      const identity = [
        episode.id,
        kind,
        val.path || buildSubtitleLanguageKey(val),
      ];
      const key = JSON.stringify(identity);
      const occurrence = occurrences.get(key) ?? 0;
      occurrences.set(key, occurrence + 1);
      return JSON.stringify([...identity, occurrence]);
    };

    const missing = episode.missing_subtitles.map((val) => (
      <Subtitle
        missing
        key={subtitleKey(val, "missing")}
        seriesId={seriesId}
        episodeId={episodeId}
        localEpisodeId={episode.id}
        arrInstanceId={episode.arr_instance_id}
        subtitle={val}
        availableSubtitles={episode.subtitles}
      ></Subtitle>
    ));

    let filteredSubtitles = episode.subtitles;
    if (onlyDesired) {
      filteredSubtitles = filterSubtitleBy(filteredSubtitles, profileItems);
    }

    // Files before tracks. A release can carry several dozen embedded
    // tracks and a handful of files, and the files are the ones a reader
    // can act on, so they must not be the ones an overflow cuts off.
    const files = filteredSubtitles.filter((val) => val.path);
    const embedded = filteredSubtitles.filter((val) => !val.path);

    const render = (val: Subtitle, kind: string) => (
      <Subtitle
        key={subtitleKey(val, kind)}
        seriesId={seriesId}
        episodeId={episodeId}
        localEpisodeId={episode.id}
        arrInstanceId={episode.arr_instance_id}
        subtitle={val}
        availableSubtitles={episode.subtitles}
        scoreInfo={scoreFor(val)}
      ></Subtitle>
    );

    return [
      ...missing,
      ...files.map((val) => render(val, "file")),
      ...embedded.map((val) => render(val, "embedded")),
    ];
  }, [
    episode,
    seriesId,
    onlyDesired,
    profileItems,
    historyMap,
    embeddedScoreMap,
  ]);

  const visible = elements.slice(0, MAX_CELL_SUBTITLES);
  const overflow = elements.length - visible.length;

  return (
    <Group gap="xs" wrap="wrap">
      {visible}
      {overflow > 0 && (
        <SubtitlesOverflow count={overflow}>
          {elements.slice(MAX_CELL_SUBTITLES)}
        </SubtitlesOverflow>
      )}
      <HiddenTracks
        mediaType="episode"
        mediaId={episode.id}
        batchIds={episodeIds}
        disabled={disabled}
      />
    </Group>
  );
});

const ActionsCell = ({ episode }: { episode: Item.Episode }) => {
  const { disabled, download } = useEpisodesTable();
  const modals = useModals();

  const episodeAvailableLangs = Array.from(
    new Set(
      (episode.subtitles ?? [])
        .filter(
          (s) =>
            s.path && !isSyncOutputSubtitle(s) && !isCombinedOutputSubtitle(s),
        )
        .map((s) => s.code2),
    ),
  );
  return (
    <Group gap="xs" wrap="nowrap">
      <Action
        label="Manual Search"
        disabled={disabled}
        className={tableStyles.actionIcon}
        onClick={() => {
          modals.openContextModal(EpisodeSearchModal, {
            item: episode,
            download,
            query: useEpisodesProvider,
          });
        }}
        icon={faMagnifyingGlass}
      ></Action>
      <Action
        label="Combine Subtitles"
        disabled={disabled || episodeAvailableLangs.length < 2}
        className={tableStyles.actionIcon}
        onClick={() => {
          modals.openContextModal(CombineModal, {
            scope: {
              kind: "episode",
              episodeId: episode.sonarrEpisodeId,
              arrInstanceId: episode.arr_instance_id ?? undefined,
            },
            availableLanguages: episodeAvailableLangs,
          });
        }}
        icon={faLayerGroup}
      ></Action>
      <Action
        label="History"
        disabled={disabled}
        className={tableStyles.actionIcon}
        onClick={() => {
          modals.openContextModal(
            EpisodeHistoryModal,
            {
              episode,
            },
            {
              title: `History - ${episode.title}`,
            },
          );
        }}
        icon={faHistory}
      ></Action>
    </Group>
  );
};

const columns: ColumnDef<Item.Episode>[] = [
  {
    id: "monitored",
    cell: ({
      row: {
        original: { monitored },
      },
    }) => {
      return (
        <Tooltip
          label={monitored ? "Monitored in Sonarr" : "Unmonitored in Sonarr"}
        >
          <FontAwesomeIcon icon={monitored ? faBookmark : farBookmark} />
        </Tooltip>
      );
    },
  },
  {
    header: "",
    accessorKey: "season",
    cell: ({
      row: {
        original: { season },
      },
    }) => {
      return <Text span>Season {season}</Text>;
    },
  },
  {
    header: "Episode",
    accessorKey: "episode",
    cell: ({
      row: {
        original: { episode },
      },
    }) => {
      return <span className={tableStyles.episodeNumber}>{episode}</span>;
    },
  },
  {
    header: "Title",
    accessorKey: "title",
    cell: ({
      row: {
        original: { sceneName, title },
      },
    }) => {
      return (
        <TextPopover text={sceneName}>
          <Text className={`table-primary ${tableStyles.episodeTitle}`}>
            {title}
          </Text>
        </TextPopover>
      );
    },
  },
  {
    header: "Audio",
    accessorKey: "audio_language",
    cell: ({
      row: {
        original: { audio_language: audioLanguage },
      },
    }) => <AudioList audios={audioLanguage}></AudioList>,
  },
  {
    header: "Subtitles",
    accessorKey: "missing_subtitles",
    cell: SubtitlesCell,
  },
  {
    header: "Actions",
    cell: ({ row: { original } }) => <ActionsCell episode={original} />,
  },
];

// The local episode id, so a row keeps its id, and its mounted cells, when an
// episode sorted before it is added or removed. The default is the position
// in the list.
const getEpisodeRowId = (episode: Item.Episode) => String(episode.id);

const Table = forwardRef<TableInstance<Item.Episode> | null, Props>(
  ({ episodes, history, profile, disabled, onAllRowsExpandedChanged }, ref) => {
    const onlyDesired = useShowOnlyDesired();

    const orderedHistory = useMemo(
      () =>
        [...(history ?? [])].sort((a, b) => {
          if (
            a.timestamp_iso &&
            b.timestamp_iso &&
            a.timestamp_iso !== b.timestamp_iso
          ) {
            return a.timestamp_iso > b.timestamp_iso ? -1 : 1;
          }
          return (b.history_id ?? 0) - (a.history_id ?? 0);
        }),
      [history],
    );

    // Score for a downloaded subtitle: keyed by (episode local id, file path)
    // from the newest file mutation, including replacements and deletions.
    const historyMap = useMemo(() => {
      const map = new Map<string, History.Episode>();
      orderedHistory.forEach((h) => {
        if (!h.subtitles_path) return;
        if ([0, 1, 2, 3, 4, 6].includes(h.action)) {
          const key = JSON.stringify([h.id, h.subtitles_path]);
          if (!map.has(key)) map.set(key, h);
        }
      });
      return map;
    }, [orderedHistory]);

    // Score for an embedded track: keyed by (episode local id, language key)
    // from the "treat embedded as downloaded" action=7 records.
    const embeddedScoreMap = useMemo(() => {
      const map = new Map<string, History.Episode>();
      orderedHistory.forEach((h) => {
        if (h.action === 7 && h.language?.code2) {
          const key = JSON.stringify([
            h.id,
            buildHistoryLanguageKey(h.language),
          ]);
          if (!map.has(key)) map.set(key, h);
        }
      });
      return map;
    }, [orderedHistory]);

    const tableRef =
      ref as React.MutableRefObject<TableInstance<Item.Episode> | null>;

    const profileItems = useProfileItemsToLanguages(profile);

    const { mutateAsync } = useDownloadEpisodeSubtitles();

    const episodeIds = useMemo(
      () => (episodes ?? []).map((episode) => episode.id),
      [episodes],
    );

    const download = useCallback(
      async (item: Item.Episode, result: SearchResultType) => {
        const {
          language,
          hearing_impaired: hi,
          forced,
          provider,
          subtitle,
          original_format: originalFormat,
        } = result;
        const { sonarrSeriesId: seriesId, sonarrEpisodeId: episodeId } = item;

        await mutateAsync({
          seriesId,
          episodeId,
          // Scope the download to the episode's owning instance (#156); the
          // backend dual-uses the upstream ids + this to disambiguate.
          arrInstanceId: item.arr_instance_id,
          form: {
            language,
            hi,
            forced,
            provider,
            subtitle,
            // eslint-disable-next-line camelcase
            original_format: originalFormat,
          },
        });
      },
      [mutateAsync],
    );

    const maxSeason = useMemo(
      () =>
        episodes?.reduce<number>(
          (prev, curr) => Math.max(prev, curr.season),
          0,
        ) ?? 0,
      [episodes],
    );

    useEffect(() => {
      tableRef?.current?.setExpanded(() => ({ [`season:${maxSeason}`]: true }));
    }, [tableRef, maxSeason]);

    const tableValue = useMemo<EpisodesTableValue>(
      () => ({
        disabled,
        episodeIds,
        onlyDesired,
        profileItems,
        historyMap,
        embeddedScoreMap,
        download,
      }),
      [
        disabled,
        episodeIds,
        onlyDesired,
        profileItems,
        historyMap,
        embeddedScoreMap,
        download,
      ],
    );

    return (
      <EpisodesTableContext.Provider value={tableValue}>
        <GroupTable
          columns={columns}
          data={episodes ?? []}
          getRowId={getEpisodeRowId}
          instanceRef={tableRef}
          onAllRowsExpandedChanged={onAllRowsExpandedChanged}
          initialState={{
            sorting: [
              { id: "season", desc: true },
              { id: "episode", desc: true },
            ],
            grouping: ["season"],
          }}
          tableStyles={{ emptyText: "No Episode Found For This Series" }}
        ></GroupTable>
      </EpisodesTableContext.Provider>
    );
  },
);

export default Table;
