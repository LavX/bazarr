import React, {
  forwardRef,
  FunctionComponent,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useNavigate } from "react-router";
import { Badge, Group, Text, Tooltip, UnstyledButton } from "@mantine/core";
import { faBookmark as farBookmark } from "@fortawesome/free-regular-svg-icons";
import {
  faBookmark,
  faHistory,
  faLayerGroup,
  faMagnifyingGlass,
  faSync,
} from "@fortawesome/free-solid-svg-icons";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { ColumnDef, Table as TableInstance } from "@tanstack/react-table";
import { useSubtitleFileDownload } from "@/apis/hooks";
import { useCombineSubtitles } from "@/apis/hooks/combine";
import { useSportsSubtitleModification } from "@/apis/hooks/sports";
import { SportsEvent } from "@/apis/raw/sports";
import { Action, GroupTable } from "@/components";
import { AudioList, CombinedSubtitleBadge } from "@/components/bazarr";
import { SportsSearchModal } from "@/components/modals/SportsSearchModal";
import SyncOutputCompareModal from "@/components/modals/SyncOutputCompareModal";
import SubtitleToolsMenu from "@/components/SubtitleToolsMenu";
import TextPopover from "@/components/TextPopover";
import { useModals } from "@/modules/modals";
import { BuildKey, toPython } from "@/utilities";
import {
  buildSubtitleLanguageKey,
  canSynchronizeSubtitle,
  combineRequestForSubtitle,
  isCombinedOutputSubtitle,
  isCompatibleSyncOutputSubtitle,
  isSyncOutputSubtitle,
  sortSyncOutputSubtitles,
} from "@/utilities/subtitles";
import { navigateApp } from "@/utilities/whatsNew";
import tableStyles from "@/components/tables/BaseTable.module.scss";

interface Props {
  events: SportsEvent[] | null;
  disabled?: boolean;
  indexingId?: number;
  onIndex: (event: SportsEvent) => void;
  onAllRowsExpandedChanged: (isAllRowsExpanded: boolean) => void;
}

type SportsSubtitleTuple = SportsEvent["subtitles"][number];

// Sports keeps a subtitle as a [language, path, size] tuple where episodes and
// movies hold a Subtitle object, so the tuple is widened here rather than
// teaching the shared menu a second shape. The language key carries the variant
// flags, "en", "en:hi", "en:sync-ffsubsync", "en:combined-es", which is the same
// convention the other two media types use, so the shared helpers read it as is.
export function toSportsSubtitle([
  language,
  path,
]: SportsSubtitleTuple): Subtitle {
  const [code2, ...modifiers] = language.split(":");
  const lower = modifiers.map((modifier) => modifier.toLowerCase());
  return {
    code2,
    name: code2,
    language,
    modifier:
      modifiers.find((modifier) => {
        const value = modifier.toLowerCase();
        return value.startsWith("sync-") || value.startsWith("combined-");
      }) ?? null,
    forced: lower.includes("forced"),
    hi: lower.includes("hi"),
    path,
  };
}

// The sports counterpart of buildEpisodeSubtitleToolSelections. The id is the
// local event id, which is what the modify endpoint resolves a sports row from,
// and every selection carries its owner because a sports path mapping is always
// per instance with no global mapping to fall back on.
export function buildSportsSubtitleToolSelections(
  event: SportsEvent,
  subtitle: Subtitle,
): FormType.ModifySubtitle[] {
  const isEmbedded = !subtitle.path;
  return [
    {
      id: event.id,
      type: "sports",
      path: isEmbedded ? "" : subtitle.path!,
      language: subtitle.code2,
      forced: toPython(subtitle.forced),
      hi: toPython(subtitle.hi),
      from_language: isEmbedded ? subtitle.code2 : undefined,
      // eslint-disable-next-line camelcase
      arr_instance_id: event.arr_instance_id,
    },
  ];
}

// One sports subtitle behind the same tools menu episodes and movies use.
// This column used to render bare badges: a missing language opened a search and
// a present one did nothing at all, so a subtitle that existed could not be
// viewed, edited, downloaded, translated, synced or deleted from the only page
// that lists it. The menu is the shared component rather than a sports copy, so
// the tool groups and their wording stay identical across the three media types.
const EventSubtitleBadge: FunctionComponent<{
  event: SportsEvent;
  subtitle: Subtitle;
  missing?: boolean;
  availableSubtitles: Subtitle[];
}> = ({ event, subtitle, missing = false, availableSubtitles }) => {
  const navigate = useNavigate();
  const modals = useModals();
  const combine = useCombineSubtitles();
  const fileDownload = useSubtitleFileDownload();
  const { remove } = useSportsSubtitleModification();

  const isEmbedded = !subtitle.path;
  const isCombinedOutput = !missing && isCombinedOutputSubtitle(subtitle);

  const [compareOpened, setCompareOpened] = useState(false);

  // A missing language has no file to act on, so it carries no selections: the
  // menu then offers only what applies, which is a search and a translate from
  // one of the subtitles the event already has.
  const selections = useMemo(
    () => (missing ? [] : buildSportsSubtitleToolSelections(event, subtitle)),
    [event, subtitle, missing],
  );

  const translationSources = useMemo(
    () =>
      availableSubtitles.filter(
        (item) =>
          !isSyncOutputSubtitle(item) && !isCombinedOutputSubtitle(item),
      ),
    [availableSubtitles],
  );

  // The sync outputs of THIS subtitle: same base language and variant flags,
  // only the sync-engine modifier differs. Mirrors the episodes component so
  // the compare action means the same thing on all three media types.
  const syncOutputs = useMemo(
    () =>
      sortSyncOutputSubtitles(
        availableSubtitles.filter((item) =>
          isCompatibleSyncOutputSubtitle(subtitle, item),
        ),
      ),
    [availableSubtitles, subtitle],
  );

  const canCompareSyncOutputs =
    !missing &&
    !isEmbedded &&
    !isSyncOutputSubtitle(subtitle) &&
    syncOutputs.length > 0;

  const editorUrl = (action: "preview" | "edit") =>
    `/subtitles/${action}/sports/${event.id}/${encodeURIComponent(
      buildSubtitleLanguageKey(subtitle),
    )}?arr_instance_id=${event.arr_instance_id}`;

  const badgeEl = missing ? (
    <Badge
      color="yellow"
      variant="light"
      style={{ cursor: event.hasFile ? "pointer" : undefined }}
      leftSection={<FontAwesomeIcon icon={faMagnifyingGlass} />}
    >
      {subtitle.language ?? subtitle.code2}
    </Badge>
  ) : isCombinedOutput ? (
    <CombinedSubtitleBadge subtitle={subtitle} />
  ) : (
    // The raw language key, as this column has always shown it: it already
    // carries the variant, so "en:hi" and "en:sync-ffsubsync" say what they are
    // without a second badge.
    <Badge
      variant="light"
      style={{ cursor: "pointer", whiteSpace: "nowrap" }}
      title={subtitle.path || "Embedded"}
    >
      {subtitle.language ?? subtitle.code2}
    </Badge>
  );

  return (
    <>
      <SubtitleToolsMenu
        selections={selections}
        menu={{ trigger: "click" }}
        canSync={!missing && canSynchronizeSubtitle(subtitle)}
        canCompareSyncOutputs={canCompareSyncOutputs}
        isCombinedOutput={isCombinedOutput}
        missingLanguage={missing ? subtitle : undefined}
        translationSources={missing ? translationSources : undefined}
        mediaId={event.id}
        mediaType="sports"
        arrInstanceId={event.arr_instance_id}
        embeddedTrack={isEmbedded}
        onAction={async (action) => {
          if (action === "view") {
            navigate(editorUrl("preview"));
          } else if (action === "edit") {
            navigate(editorUrl("edit"));
          } else if (action === "compare-sync") {
            setCompareOpened(true);
          } else if (action === "search") {
            if (!event.hasFile) return;
            modals.openContextModal(SportsSearchModal, {
              item: event,
              language: subtitle.code2,
              hi: subtitle.hi,
              forced: subtitle.forced,
            });
          } else if (action === "download") {
            fileDownload.mutate({
              type: "sports",
              mediaId: event.id,
              language: buildSubtitleLanguageKey(subtitle),
              arrInstanceId: event.arr_instance_id,
            });
          } else if (action === "rebuild") {
            combine.mutate({
              scope: {
                kind: "sports",
                eventId: event.id,
                arrInstanceId: event.arr_instance_id,
              },
              body: combineRequestForSubtitle(subtitle) ?? {},
            });
          } else if (action === "delete" && subtitle.path) {
            await remove.mutateAsync({
              eventId: event.id,
              owner: event.arr_instance_id,
              form: {
                language: subtitle.code2,
                hi: subtitle.hi,
                forced: subtitle.forced,
                path: subtitle.path,
              },
            });
          }
        }}
      >
        {isCombinedOutput ? (
          <UnstyledButton aria-label={`Combined subtitle ${subtitle.code2}`}>
            {badgeEl}
          </UnstyledButton>
        ) : (
          badgeEl
        )}
      </SubtitleToolsMenu>
      {canCompareSyncOutputs && (
        <SyncOutputCompareModal
          opened={compareOpened}
          onClose={() => setCompareOpened(false)}
          mediaType="sports"
          mediaId={event.id}
          arrInstanceId={event.arr_instance_id}
          original={subtitle}
          outputs={syncOutputs}
        />
      )}
    </>
  );
};

// Extracted for the same reason as EventRowActions below: it needs the modals
// hook, and hook objects change identity every render.
const EventSubtitles: FunctionComponent<{ event: SportsEvent }> = ({
  event,
}) => {
  // The subtitles the event already has are the translate sources for the ones
  // it does not, so the tuples are widened once and read by both lists.
  const presentSubtitles = useMemo(
    () => (event.subtitles ?? []).map(toSportsSubtitle),
    [event.subtitles],
  );

  return (
    <Group gap="xs" wrap="nowrap">
      {event.missing_subtitles?.map((language, index) => (
        <EventSubtitleBadge
          key={BuildKey(index, language, "missing")}
          event={event}
          subtitle={toSportsSubtitle([language, null, null])}
          availableSubtitles={presentSubtitles}
          missing
        />
      ))}
      {presentSubtitles.map((subtitle, index) => (
        <EventSubtitleBadge
          key={BuildKey(index, subtitle.language ?? subtitle.code2, "present")}
          event={event}
          subtitle={subtitle}
          availableSubtitles={presentSubtitles}
        />
      ))}
      {!event.missing_subtitles?.length && !event.subtitles?.length ? (
        <Text size="sm" c="dimmed">
          {event.profileId == null ? "No profile" : "None"}
        </Text>
      ) : null}
    </Group>
  );
};

// Its own component rather than an inline cell. Built inline it needed the
// modals and combine hooks in the columns useMemo deps, and those objects get a
// new identity on every render, so the whole column set was rebuilt each time
// and anything the row had opened, a menu or a modal, was unmounted underneath
// the user.
const EventRowActions: FunctionComponent<{
  event: SportsEvent;
  disabled?: boolean;
  indexing: boolean;
  onIndex: (event: SportsEvent) => void;
}> = ({ event, disabled, indexing, onIndex }) => {
  const modals = useModals();
  const combine = useCombineSubtitles();

  return (
    <Group gap="xs" wrap="nowrap">
      <Action
        label="Manual Search"
        disabled={disabled || !event.hasFile}
        className={tableStyles.actionIcon}
        onClick={() => {
          modals.openContextModal(SportsSearchModal, { item: event });
        }}
        icon={faMagnifyingGlass}
      ></Action>
      <Action
        label="Index Subtitles"
        disabled={disabled || !event.hasFile}
        loading={indexing}
        className={tableStyles.actionIcon}
        onClick={() => onIndex(event)}
        icon={faSync}
      ></Action>
      <Action
        label="Combine Subtitles"
        disabled={disabled || combine.isPending || !event.hasFile}
        loading={combine.isPending}
        className={tableStyles.actionIcon}
        onClick={() =>
          combine.mutate({
            scope: {
              kind: "sports",
              eventId: event.id,
              arrInstanceId: event.arr_instance_id,
            },
            // No languages or format: a sports composition follows the combine
            // rule on its league's profile, and the engine refuses an override
            // alongside an owned publication.
            body: {},
          })
        }
        icon={faLayerGroup}
      ></Action>
      <Action
        label="History"
        className={tableStyles.actionIcon}
        onClick={() =>
          navigateApp(
            `/history/sports?instance=${event.arr_instance_id}&event_id=${event.id}`,
          )
        }
        icon={faHistory}
      ></Action>
    </Group>
  );
};

// Mirrors the Episodes table: same grouping by season, same column order, same
// icon actions. Sports events carry their subtitles as [language, path] tuples
// rather than the Subtitle objects episodes use, so the cells render badges
// directly instead of reusing the episode Subtitle component.
const Table = forwardRef<TableInstance<SportsEvent> | null, Props>(
  (
    { events, disabled, indexingId, onIndex, onAllRowsExpandedChanged },
    ref,
  ) => {
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
          header: "Audio",
          accessorKey: "audio_language",
          cell: ({ row: { original } }) => (
            // The indexer reads these off the file and the table never showed
            // them, though which audio an event carries is exactly what
            // decides whether a subtitle is wanted. Bare codes here, not the
            // Language.Info objects the episodes rows carry.
            <AudioList
              audios={(original.audio_language ?? []).map((code) => ({
                code2: code,
                name: code,
              }))}
            ></AudioList>
          ),
        },
        {
          header: "Subtitles",
          accessorKey: "missing_subtitles",
          cell: ({ row: { original } }) => <EventSubtitles event={original} />,
        },
        {
          header: "Actions",
          cell: ({ row: { original } }) => (
            <EventRowActions
              event={original}
              disabled={disabled}
              indexing={indexingId === original.id}
              onIndex={onIndex}
            />
          ),
        },
      ],
      [disabled, indexingId, onIndex],
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
