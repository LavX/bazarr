import React, { forwardRef, useCallback, useEffect, useMemo } from "react";
import { Badge, Group, Popover, Text, Tooltip } from "@mantine/core";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faHistory,
  faLayerGroup,
  faMagnifyingGlass,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Table as TableInstance } from "@tanstack/react-table";
import { useDownloadEpisodeSubtitles, useEpisodesProvider } from "@/apis/hooks";
import { useShowOnlyDesired } from "@/apis/hooks/site";
import { Action, GroupTable } from "@/components";
import { AudioList } from "@/components/bazarr";
import { CombineModal } from "@/components/forms/CombineForm";
import { EpisodeHistoryModal } from "@/components/modals";
import { EpisodeSearchModal } from "@/components/modals/ManualSearchModal";
import TextPopover from "@/components/TextPopover";
import { useModals } from "@/modules/modals";
import { BuildKey, filterSubtitleBy } from "@/utilities";
import { useProfileItemsToLanguages } from "@/utilities/languages";
import {
  isCombinedOutputSubtitle,
  isSyncOutputSubtitle,
} from "@/utilities/subtitles";
import { Subtitle } from "./components";

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
  disabled?: boolean;
  profile?: Language.Profile;
  onAllRowsExpandedChanged: (isAllRowsExpanded: boolean) => void;
}

const Table = forwardRef<TableInstance<Item.Episode> | null, Props>(
  ({ episodes, profile, disabled, onAllRowsExpandedChanged }, ref) => {
    const onlyDesired = useShowOnlyDesired();

    const tableRef =
      ref as React.MutableRefObject<TableInstance<Item.Episode> | null>;

    const profileItems = useProfileItemsToLanguages(profile);

    const { mutateAsync } = useDownloadEpisodeSubtitles();

    const modals = useModals();

    const download = useCallback(
      (item: Item.Episode, result: SearchResultType) => {
        const {
          language,
          hearing_impaired: hi,
          forced,
          provider,
          subtitle,
          original_format: originalFormat,
        } = result;
        const { sonarrSeriesId: seriesId, sonarrEpisodeId: episodeId } = item;

        return mutateAsync({
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

    const SubtitlesCell = React.memo(
      ({ episode }: { episode: Item.Episode }) => {
        const seriesId = episode.sonarrSeriesId;

        const elements = useMemo(() => {
          const episodeId = episode.sonarrEpisodeId;

          const missing = episode.missing_subtitles.map((val, idx) => (
            <Subtitle
              missing
              key={BuildKey(idx, val.code2, "missing")}
              seriesId={seriesId}
              episodeId={episodeId}
              arrInstanceId={episode.arr_instance_id}
              subtitle={val}
              availableSubtitles={episode.subtitles}
            ></Subtitle>
          ));

          let filteredSubtitles = episode.subtitles;
          if (onlyDesired) {
            filteredSubtitles = filterSubtitleBy(
              filteredSubtitles,
              profileItems,
            );
          }

          // Files before tracks. A release can carry several dozen embedded
          // tracks and a handful of files, and the files are the ones a reader
          // can act on, so they must not be the ones an overflow cuts off.
          const files = filteredSubtitles.filter((val) => val.path);
          const embedded = filteredSubtitles.filter((val) => !val.path);

          const render = (val: Subtitle, idx: number, kind: string) => (
            <Subtitle
              key={BuildKey(idx, val.code2, kind)}
              seriesId={seriesId}
              episodeId={episodeId}
              arrInstanceId={episode.arr_instance_id}
              subtitle={val}
              availableSubtitles={episode.subtitles}
            ></Subtitle>
          );

          return [
            ...missing,
            ...files.map((val, idx) => render(val, idx, "file")),
            ...embedded.map((val, idx) => render(val, idx, "embedded")),
          ];
          // onlyDesired/profileItems are captured from the parent; the row re-renders
          // via the parent when they change, so they belong in the deps.
          // eslint-disable-next-line react-hooks/exhaustive-deps
        }, [episode, seriesId, onlyDesired, profileItems]);

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
          </Group>
        );
      },
    );

    const columns = useMemo<ColumnDef<Item.Episode>[]>(
      () => [
        {
          id: "monitored",
          cell: ({
            row: {
              original: { monitored },
            },
          }) => {
            return (
              <Tooltip
                label={
                  monitored ? "Monitored in Sonarr" : "Unmonitored in Sonarr"
                }
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
          cell: ({ row: { original } }) => {
            return <SubtitlesCell episode={original} />;
          },
        },
        {
          header: "Actions",
          cell: ({ row }) => {
            const episodeAvailableLangs = Array.from(
              new Set(
                (row.original.subtitles ?? [])
                  .filter(
                    (s) =>
                      s.path &&
                      !isSyncOutputSubtitle(s) &&
                      !isCombinedOutputSubtitle(s),
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
                      item: row.original,
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
                        episodeId: row.original.sonarrEpisodeId,
                        arrInstanceId:
                          row.original.arr_instance_id ?? undefined,
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
                        episode: row.original,
                      },
                      {
                        title: `History - ${row.original.title}`,
                      },
                    );
                  }}
                  icon={faHistory}
                ></Action>
              </Group>
            );
          },
        },
      ],
      [disabled, download, modals, SubtitlesCell],
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

    return (
      <GroupTable
        columns={columns}
        data={episodes ?? []}
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
    );
  },
);

export default Table;
