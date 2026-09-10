import React, { forwardRef, useEffect, useMemo } from "react";
import { Badge, Group, Text, Tooltip } from "@mantine/core";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faHistory,
  faMagnifyingGlass,
  faSync,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Table as TableInstance } from "@tanstack/react-table";
import { SportsEvent } from "@/apis/raw/sports";
import { Action, GroupTable } from "@/components";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import TextPopover from "@/components/TextPopover";
import { useModals } from "@/modules/modals";
import { BuildKey } from "@/utilities";
import { navigateApp } from "@/utilities/whatsNew";
import tableStyles from "@/components/tables/BaseTable.module.scss";

interface Props {
  events: SportsEvent[] | null;
  disabled?: boolean;
  indexingId?: number;
  onIndex: (event: SportsEvent) => void;
  onAllRowsExpandedChanged: (isAllRowsExpanded: boolean) => void;
}

// Mirrors the Episodes table: same grouping by season, same column order, same
// icon actions. Sports events carry their subtitles as [language, path] tuples
// rather than the Subtitle objects episodes use, so the cells render badges
// directly instead of reusing the episode Subtitle component.
const Table = forwardRef<TableInstance<SportsEvent> | null, Props>(
  (
    { events, disabled, indexingId, onIndex, onAllRowsExpandedChanged },
    ref,
  ) => {
    const modals = useModals();
    const tableRef =
      ref as React.MutableRefObject<TableInstance<SportsEvent> | null>;

    const columns = useMemo<ColumnDef<SportsEvent>[]>(
      () => [
        {
          id: "monitored",
          cell: ({ row: { original } }) => (
            <Tooltip
              label={
                original.hasFile ? "File available" : "No file in Sportarr"
              }
            >
              <FontAwesomeIcon
                icon={original.hasFile ? faBookmark : farBookmark}
              />
            </Tooltip>
          ),
        },
        {
          // Grouped on a normalised season: unlike an episode, a sports event
          // can legitimately have none, and grouping on a null key produces a
          // group the expanded-row state can never address, so every row stays
          // collapsed and the table looks empty.
          header: "",
          id: "season",
          accessorFn: (row) => row.season ?? 0,
          cell: ({ row: { original } }) => (
            <Text span>
              {original.season == null
                ? "Unknown season"
                : `Season ${original.season}`}
            </Text>
          ),
        },
        {
          header: "Event",
          id: "episode",
          accessorFn: (row) => row.episode ?? 0,
          cell: ({ row: { original } }) => (
            <span className={tableStyles.episodeNumber}>
              {original.episode ?? "-"}
            </span>
          ),
        },
        {
          header: "Title",
          accessorKey: "title",
          cell: ({ row: { original } }) => {
            const part =
              original.partName ||
              (original.partNumber && original.partNumber > 0
                ? `Part ${original.partNumber}`
                : null);
            // Built as one string, not two adjacent JSX children: React would
            // otherwise emit separate text nodes and the title would no longer
            // be matchable (or selectable) as a single label.
            const label = part ? `${original.title} (${part})` : original.title;
            return (
              <TextPopover text={original.sceneName}>
                <Text className={`table-primary ${tableStyles.episodeTitle}`}>
                  {label}
                </Text>
              </TextPopover>
            );
          },
        },
        {
          header: "Date",
          accessorKey: "eventDate",
          cell: ({ row: { original } }) => (
            <Text size="sm" c="dimmed">
              {(original.broadcastDate || original.eventDate)?.slice(0, 10) ||
                "Unknown"}
            </Text>
          ),
        },
        {
          header: "Subtitles",
          accessorKey: "missing_subtitles",
          cell: ({ row: { original } }) => (
            <Group gap="xs" wrap="nowrap">
              {original.missing_subtitles?.map((language, index) => {
                // Clicking a missing language searches for that one, the way
                // the episodes table and the wanted pages do. The badges used
                // to be inert, so the only way to act on one missing language
                // was the row's search, which searches every missing language.
                const [code2, ...modifiers] = language.split(":");
                const lower = modifiers.map((modifier) =>
                  modifier.toLowerCase(),
                );
                return (
                  <Badge
                    key={BuildKey(index, language, "missing")}
                    color="yellow"
                    variant="light"
                    style={{ cursor: original.hasFile ? "pointer" : undefined }}
                    leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
                    onClick={() => {
                      if (!original.hasFile) return;
                      modals.openContextModal(SportsSearchModal, {
                        item: original,
                        language: code2,
                        hi: lower.includes("hi"),
                        forced: lower.includes("forced"),
                      });
                    }}
                  >
                    {language}
                  </Badge>
                );
              })}
              {original.subtitles?.map(([language, path], index) => (
                <Badge
                  key={BuildKey(index, language, "present")}
                  variant="light"
                  title={path || "Embedded"}
                >
                  {language}
                </Badge>
              ))}
              {!original.missing_subtitles?.length &&
              !original.subtitles?.length ? (
                <Text size="sm" c="dimmed">
                  {original.profileId == null ? "No profile" : "None"}
                </Text>
              ) : null}
            </Group>
          ),
        },
        {
          header: "Actions",
          cell: ({ row: { original } }) => (
            <Group gap="xs" wrap="nowrap">
              <Action
                label="Manual Search"
                disabled={disabled || !original.hasFile}
                className={tableStyles.actionIcon}
                onClick={() => {
                  modals.openContextModal(SportsSearchModal, {
                    item: original,
                  });
                }}
                icon={faMagnifyingGlass}
              ></Action>
              <Action
                label="Index Subtitles"
                disabled={disabled || !original.hasFile}
                loading={indexingId === original.id}
                className={tableStyles.actionIcon}
                onClick={() => onIndex(original)}
                icon={faSync}
              ></Action>
              <Action
                label="History"
                className={tableStyles.actionIcon}
                onClick={() =>
                  navigateApp(
                    `/history/sports?instance=${original.arr_instance_id}&event_id=${original.id}`,
                  )
                }
                icon={faHistory}
              ></Action>
            </Group>
          ),
        },
      ],
      [disabled, indexingId, onIndex, modals],
    );

    const maxSeason = useMemo(
      () =>
        events?.reduce<number>(
          (prev, curr) => Math.max(prev, curr.season ?? 0),
          0,
        ) ?? 0,
      [events],
    );

    useEffect(() => {
      tableRef?.current?.setExpanded(() => ({ [`season:${maxSeason}`]: true }));
    }, [tableRef, maxSeason]);

    return (
      <GroupTable
        columns={columns}
        data={events ?? []}
        instanceRef={tableRef}
        onAllRowsExpandedChanged={onAllRowsExpandedChanged}
        initialState={{
          sorting: [
            { id: "season", desc: true },
            { id: "episode", desc: true },
          ],
          grouping: ["season"],
        }}
        tableStyles={{ emptyText: "No Event Found For This League" }}
      ></GroupTable>
    );
  },
);

export default Table;
